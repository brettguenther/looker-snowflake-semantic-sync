"""End-to-end tests for SyncOrchestrator including semantic diff tracking and Two-Layer Refinement boundary."""

from pathlib import Path
from typing import List, Optional

from snowflake_semantic_sync.models.snowflake import (
    SnowflakeSemanticView,
    parse_semantic_view_yaml,
)
from snowflake_semantic_sync.orchestrator import SyncOrchestrator, load_sync_config

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_semantic_view.yaml"


class FakeSnowflakeClient:
    def list_semantic_views(self, database: str, schema: str) -> List[str]:
        return ["TPCH_REV_ANALYSIS"]

    def get_semantic_views(
        self,
        database: str,
        schema: str,
        view_names: Optional[List[str]] = None,
    ) -> List[SnowflakeSemanticView]:
        raw_yaml = FIXTURE_PATH.read_text(encoding="utf-8")
        return [parse_semantic_view_yaml(raw_yaml, database=database, schema_name=schema)]


def test_orchestrator_end_to_end_and_idempotent_diff(tmp_path: Path):
    out_dir = tmp_path / "output"
    orchestrator = SyncOrchestrator(
        config_path="config/sync_config.yaml",
        snowflake_client=FakeSnowflakeClient(),
        snowflake_database="ANALYTICS_DB",
        snowflake_schema="GOLD",
        model_name="snowflake_semantic_model",
        output_dir=str(out_dir),
    )

    # First run with scaffold=True
    res1 = orchestrator.run(deploy=False, create_pr=False, scaffold=True)
    assert res1["semantic_views_processed"] == 1
    assert res1["views_generated"] == ["tpch_rev_analysis"]
    assert res1["explores_generated"] == ["tpch_rev_analysis"]
    assert len(res1["files_changed"]) == 2
    assert len(res1["files_unchanged"]) == 0

    base_view_file = out_dir / "views" / "base" / "tpch_rev_analysis.base.view.lkml"
    curated_view_file = out_dir / "views" / "curated" / "tpch_rev_analysis.view.lkml"
    explore_file = out_dir / "explores" / "base" / "tpch_rev_analysis.base.explore.lkml"
    model_file = out_dir / "models" / "snowflake_semantic_model.model.lkml"

    assert base_view_file.exists()
    assert curated_view_file.exists()
    assert explore_file.exists()
    assert model_file.exists()

    # Simulate human edits to the curated refinement view
    custom_refinement = curated_view_file.read_text(encoding="utf-8") + "\n# Custom analyst note\n"
    curated_view_file.write_text(custom_refinement, encoding="utf-8")

    # Second run: base files are unchanged (0 changed, 2 unchanged) and curated file is untouched
    res2 = orchestrator.run(deploy=False, create_pr=False, scaffold=True)
    assert len(res2["files_changed"]) == 0
    assert len(res2["files_unchanged"]) == 2
    assert curated_view_file.read_text(encoding="utf-8") == custom_refinement


class FakeLookerClient:
    def __init__(self) -> None:
        self.dev_mode_calls = 0
        self.checked_out_branches: List[tuple[str, str]] = []
        self.written_files: dict[str, str] = {}

    def ensure_dev_mode(self) -> None:
        self.dev_mode_calls += 1

    def checkout_branch(self, project_id: str, branch: str = "master") -> None:
        self.checked_out_branches.append((project_id, branch))

    def create_or_update_file(self, project_id: str, file_path: str, content: str) -> None:
        self.written_files[f"{project_id}:{file_path}"] = content

    def validate_project(self, project_id: str) -> tuple[bool, str]:
        return True, "Project is valid."


def test_load_sync_config_honors_dotenv_over_yaml_defaults(tmp_path: Path, monkeypatch):
    for var in ("LOOKER_PROJECT_ID", "LOOKER_CONNECTION_NAME", "LOOKER_MODEL_NAME", "LOOKER_PROFILE"):
        monkeypatch.delenv(var, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "LOOKER_PROJECT_ID=my_looker_project\n"
        "LOOKER_CONNECTION_NAME=my_snowflake_conn\n"
        "LOOKER_MODEL_NAME=my_looker_model\n"
        "LOOKER_PROFILE=my-looker-profile\n",
        encoding="utf-8",
    )

    cfg = load_sync_config("config/sync_config.yaml", env_file=str(env_file))
    assert cfg.looker_project_id == "my_looker_project"
    assert cfg.connection_name == "my_snowflake_conn"
    assert cfg.model_name == "my_looker_model"
    assert cfg.looker_profile == "my-looker-profile"


def test_orchestrator_deploy_writes_only_base_files_and_preserves_dev_branch(tmp_path: Path):
    fake_looker = FakeLookerClient()
    orchestrator = SyncOrchestrator(
        config_path="config/sync_config.yaml",
        snowflake_client=FakeSnowflakeClient(),
        looker_client=fake_looker,
        snowflake_database="ANALYTICS_DB",
        snowflake_schema="GOLD",
        looker_project_id="my_looker_project",
        output_dir=str(tmp_path / "output"),
    )

    res = orchestrator.run(deploy=True, create_pr=False, scaffold=True)
    assert res["looker_deployed"] is True
    assert res["validation_valid"] is True
    assert fake_looker.dev_mode_calls == 1
    # Should NOT checkout git_base_branch ('main') when looker_dev_branch is unset
    assert fake_looker.checked_out_branches == []
    assert set(fake_looker.written_files.keys()) == {
        "my_looker_project:views/base/tpch_rev_analysis.base.view.lkml",
        "my_looker_project:explores/base/tpch_rev_analysis.base.explore.lkml",
    }

