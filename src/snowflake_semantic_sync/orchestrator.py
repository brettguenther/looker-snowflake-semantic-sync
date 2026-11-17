"""Sync Orchestrator coordinating Snowflake Semantic View extraction, LookML generation, deployment, and GitOps PRs."""

import os
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional
from dotenv import dotenv_values
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
import yaml

from snowflake_semantic_sync.clients.looker import LookerClient
from snowflake_semantic_sync.clients.snowflake import SnowflakeSemanticClient
from snowflake_semantic_sync.generator.engine import LookMLGenerator
from snowflake_semantic_sync.mapping.engine import SemanticModelMapper
from snowflake_semantic_sync.mapping.profile import MappingProfile
from snowflake_semantic_sync.models.lookml import LookMLView
from snowflake_semantic_sync.models.snowflake import (
    SnowflakeSemanticView,
    parse_semantic_view_yaml,
)
from snowflake_semantic_sync.protocols import LookMLDeployer, SnowflakeSemanticSource


def _expand_env(value: Any, env_map: Optional[Mapping[str, str]] = None) -> Any:
    """Recursively expands ${VAR} and ${VAR:-default} patterns in strings, dicts, and lists."""
    lookup = env_map if env_map is not None else os.environ
    if isinstance(value, str):
        pattern = re.compile(r"\$\{([^}^{]+)\}")

        def _repl(match: re.Match) -> str:
            expr = match.group(1)
            if ":-" in expr:
                var, default = expr.split(":-", 1)
                val = lookup.get(var)
                return val if (val is not None and val != "") else default
            return lookup.get(expr, "")

        return pattern.sub(_repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v, lookup) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, lookup) for v in value]
    return value


class SyncConfig(BaseSettings):
    """Configuration for Snowflake Semantic View -> LookML synchronization."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    snowflake_account: str = ""
    snowflake_user: str = ""
    snowflake_role: str = ""
    snowflake_warehouse: str = ""
    snowflake_database: str = ""
    snowflake_schema: str = "PUBLIC"
    snowflake_auth_mode: str = "key_pair"

    # Key-Pair credentials
    snowflake_private_key_path: str = ""
    snowflake_private_key_raw: str = ""
    snowflake_private_key_passphrase: str = ""

    # OAuth credentials
    snowflake_oauth_token: str = ""
    snowflake_oauth_client_id: str = ""
    snowflake_oauth_client_secret: str = ""
    snowflake_oauth_token_url: str = ""
    snowflake_oauth_scope: str = ""

    semantic_views: List[str] = Field(default_factory=list)

    # Looker & Git settings
    looker_project_id: str = "snowflake_semantics"
    connection_name: str = "snowflake_default"
    model_name: str = "snowflake_semantic_model"
    looker_profile: Optional[str] = None
    looker_dev_branch: Optional[str] = None
    git_repo: Optional[str] = None
    git_base_branch: str = "main"

    active_profile: str = "config/profiles/default_analytic_model.yaml"
    output_dir: str = "output"


def load_sync_config(config_path: str, env_file: str = ".env", **overrides: Any) -> SyncConfig:
    """Loads sync configuration from YAML, merging with .env and environment variables."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: '{config_path}'.")

    with open(path, "r", encoding="utf-8") as f:
        raw_cfg = yaml.safe_load(f) or {}

    # Merge .env values beneath process env vars so ${VAR:-default} in YAML honors .env
    merged_env: Dict[str, str] = {}
    env_path = Path(env_file)
    if env_path.exists():
        for k, v in dotenv_values(str(env_path)).items():
            if v is not None:
                merged_env[k] = v
    merged_env.update(os.environ)

    raw_cfg = _expand_env(raw_cfg, merged_env)
    # Remove empty-string values from YAML so BaseSettings can fall back cleanly
    cleaned_cfg: Dict[str, Any] = {}
    for k, v in raw_cfg.items():
        if v != "" and v is not None:
            cleaned_cfg[k] = v
    cleaned_cfg.update({k: v for k, v in overrides.items() if v is not None and v != ""})
    return SyncConfig(**cleaned_cfg)


class SyncOrchestrator:
    """End-to-end sync engine coordinating Snowflake Semantic View extraction and LookML Analytic Model generation."""

    def __init__(
        self,
        config_path: str = "config/sync_config.yaml",
        snowflake_client: Optional[SnowflakeSemanticSource] = None,
        looker_client: Optional[LookMLDeployer] = None,
        generator: Optional[LookMLGenerator] = None,
        **config_overrides: Any,
    ):
        self.config_path = Path(config_path)
        self.config = load_sync_config(config_path, **config_overrides)

        profile_path = Path(self.config.active_profile)
        if not profile_path.is_absolute():
            profile_path = Path.cwd() / profile_path
        if not profile_path.exists():
            raise FileNotFoundError(f"Mapping profile not found: '{profile_path}'")
        with open(profile_path, "r", encoding="utf-8") as f:
            raw_profile = yaml.safe_load(f) or {}
        self.profile = MappingProfile(**raw_profile)

        self._snowflake_client = snowflake_client
        self.mapper = SemanticModelMapper(self.profile)
        self.generator = generator or LookMLGenerator()
        self._looker_client = looker_client

    @property
    def snowflake_client(self) -> SnowflakeSemanticSource:
        if self._snowflake_client is None:
            self._snowflake_client = SnowflakeSemanticClient(
                account=self.config.snowflake_account,
                user=self.config.snowflake_user,
                role=self.config.snowflake_role,
                warehouse=self.config.snowflake_warehouse,
                database=self.config.snowflake_database,
                schema=self.config.snowflake_schema,
                auth_mode=self.config.snowflake_auth_mode,
                private_key_path=self.config.snowflake_private_key_path,
                private_key_raw=self.config.snowflake_private_key_raw,
                private_key_passphrase=self.config.snowflake_private_key_passphrase,
                oauth_token=self.config.snowflake_oauth_token,
                oauth_client_id=self.config.snowflake_oauth_client_id,
                oauth_client_secret=self.config.snowflake_oauth_client_secret,
                oauth_token_url=self.config.snowflake_oauth_token_url,
                oauth_scope=self.config.snowflake_oauth_scope,
            )
        return self._snowflake_client

    @property
    def looker_client(self) -> LookMLDeployer:
        if self._looker_client is None:
            self._looker_client = LookerClient(profile=self.config.looker_profile)
        return self._looker_client

    def run(
        self,
        deploy: bool = False,
        create_pr: bool = False,
        scaffold: bool = False,
        from_yaml_files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Executes Snowflake Semantic View extraction, LookML generation, diff tracking, and optional deployment/PR."""
        results: Dict[str, Any] = {
            "semantic_views_processed": 0,
            "views_generated": [],
            "explores_generated": [],
            "files_written": [],
            "files_changed": [],
            "files_unchanged": [],
            "looker_deployed": False,
            "validation_valid": False,
            "validation_message": "",
            "pr_result": None,
            "errors": [],
        }

        # 1. Fetch Semantic Views (from Snowflake or local YAML files)
        if from_yaml_files:
            semantic_views: List[SnowflakeSemanticView] = []
            db_name = self.config.snowflake_database or "SNOWFLAKE_DB"
            schema_name = self.config.snowflake_schema or "PUBLIC"
            for y_path in from_yaml_files:
                resolved = Path(y_path).expanduser().resolve(strict=True)
                raw_yaml = resolved.read_text(encoding="utf-8")
                semantic_views.append(
                    parse_semantic_view_yaml(
                        raw_yaml=raw_yaml,
                        database=db_name,
                        schema_name=schema_name,
                        fallback_name=resolved.stem,
                    )
                )
        else:
            if not self.config.snowflake_database:
                raise ValueError("SNOWFLAKE_DATABASE is required when syncing from Snowflake.")
            semantic_views = self.snowflake_client.get_semantic_views(
                database=self.config.snowflake_database,
                schema=self.config.snowflake_schema,
                view_names=self.config.semantic_views or None,
            )

        if not semantic_views:
            raise RuntimeError(
                f"No Snowflake Semantic Views found in {self.config.snowflake_database}.{self.config.snowflake_schema}"
            )

        out_path = Path(self.config.output_dir)
        base_views_dir = out_path / "views" / "base"
        base_explores_dir = out_path / "explores" / "base"
        base_views_dir.mkdir(parents=True, exist_ok=True)
        if self.profile.generate_base_explores:
            base_explores_dir.mkdir(parents=True, exist_ok=True)

        if scaffold:
            (out_path / "views" / "curated").mkdir(parents=True, exist_ok=True)
            (out_path / "models").mkdir(parents=True, exist_ok=True)

        views: List[LookMLView] = []
        sv_view_pairs: List[tuple[SnowflakeSemanticView, LookMLView]] = []
        managed_files: Dict[str, str] = {}

        # 2. Map & Generate Analytic Model Base Views (.base.view.lkml)
        for sv in semantic_views:
            try:
                view = self.mapper.map_semantic_view_to_lookml_view(sv)
                views.append(view)
                sv_view_pairs.append((sv, view))
                results["semantic_views_processed"] += 1
                results["views_generated"].append(view.view_name)

                base_lookml = self.generator.render_base_view(view)
                base_file_rel = f"views/base/{view.view_name}.base.view.lkml"
                base_file_local = out_path / base_file_rel

                existing_content = base_file_local.read_text(encoding="utf-8") if base_file_local.exists() else None
                if existing_content != base_lookml:
                    base_file_local.write_text(base_lookml, encoding="utf-8")
                    results["files_changed"].append(base_file_rel)
                else:
                    results["files_unchanged"].append(base_file_rel)

                results["files_written"].append(str(base_file_local))
                managed_files[base_file_rel] = base_lookml
            except Exception as e:
                err_msg = f"Failed to map/generate LookML view for '{sv.name}': {e}"
                results["errors"].append(err_msg)

        if not managed_files:
            raise RuntimeError(f"All Semantic View mappings failed. Errors: {results['errors']}")

        # 2b. Map & Generate Base Explores (.base.explore.lkml)
        if self.profile.generate_base_explores:
            for sv, view in sv_view_pairs:
                try:
                    explore = self.mapper.map_semantic_view_to_explore(sv, view)
                    explore_lookml = self.generator.render_base_explore(explore)
                    explore_file_rel = f"explores/base/{explore.name}.base.explore.lkml"
                    explore_file_local = out_path / explore_file_rel

                    existing_exp = (
                        explore_file_local.read_text(encoding="utf-8") if explore_file_local.exists() else None
                    )
                    if existing_exp != explore_lookml:
                        explore_file_local.write_text(explore_lookml, encoding="utf-8")
                        results["files_changed"].append(explore_file_rel)
                    else:
                        results["files_unchanged"].append(explore_file_rel)

                    results["explores_generated"].append(explore.name)
                    results["files_written"].append(str(explore_file_local))
                    managed_files[explore_file_rel] = explore_lookml
                except Exception as e:
                    err_msg = f"Failed to map/generate LookML explore for '{sv.name}': {e}"
                    results["errors"].append(err_msg)

        # 3. Optional local scaffolding (Starter curated refinements + model file; never overwritten or auto-PR'd)
        if scaffold:
            for view in views:
                curated_file_rel = f"views/curated/{view.view_name}.view.lkml"
                curated_file_local = out_path / curated_file_rel
                if not curated_file_local.exists():
                    curated_lookml = self.generator.render_curated_refinement(view)
                    curated_file_local.write_text(curated_lookml, encoding="utf-8")
                    results["files_written"].append(str(curated_file_local))

            model_file_rel = f"models/{self.config.model_name}.model.lkml"
            model_file_local = out_path / model_file_rel
            if not model_file_local.exists():
                model_lookml = self.generator.render_model(
                    model_name=self.config.model_name,
                    connection_name=self.config.connection_name,
                    views=views,
                )
                model_file_local.write_text(model_lookml, encoding="utf-8")
                results["files_written"].append(str(model_file_local))

        # 4. Deploy machine-managed base layer to Looker Dev Workspace & Validate
        if deploy:
            self.looker_client.ensure_dev_mode()
            if self.config.looker_dev_branch:
                self.looker_client.checkout_branch(self.config.looker_project_id, self.config.looker_dev_branch)

            for rel_path, content in managed_files.items():
                self.looker_client.create_or_update_file(
                    project_id=self.config.looker_project_id,
                    file_path=rel_path,
                    content=content,
                )

            is_valid, val_msg = self.looker_client.validate_project(self.config.looker_project_id)
            results["looker_deployed"] = True
            results["validation_valid"] = is_valid
            results["validation_message"] = val_msg

        # 5. Open or Update GitOps Pull Request (strictly scoped to machine-managed base files)
        if create_pr:
            if not self.config.git_repo:
                raise ValueError("GITHUB_REPO (or git_repo in sync_config.yaml) is required when --pr is enabled.")
            from snowflake_semantic_sync.clients.git_provider import GitHubProvider
            git_client = GitHubProvider(repo_slug=self.config.git_repo)
            pr_res = git_client.create_pull_request(
                lookml_files=managed_files,
                base_branch=self.config.git_base_branch,
            )
            results["pr_result"] = pr_res

        return results
