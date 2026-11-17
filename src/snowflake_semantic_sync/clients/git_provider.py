"""Git and GitHub Provider Client for automated branch and PR creation with open-PR deduplication."""

import base64
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request

_GITHUB_TOKEN_ENV_KEYS = ("GITHUB_TOKEN", "GITHUB_PAT", "GITHUB_PERSONAL_ACCESS_TOKEN")


class GitHubProvider:
    """Manages Git branch creation, committing, pushing, and deduplicated GitHub PR creation."""

    def __init__(self, repo_slug: str, token: Optional[str] = None):
        self.repo_slug = self._normalize_repo_slug(repo_slug)
        self.token = token or self._resolve_github_token()

    @staticmethod
    def _normalize_repo_slug(repo_slug: str) -> str:
        cleaned = (repo_slug or "").strip()
        for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        return cleaned.strip("/")

    def _resolve_github_token(self) -> Optional[str]:
        for key in _GITHUB_TOKEN_ENV_KEYS:
            if os.environ.get(key):
                return os.environ[key].strip()

        env_file = Path(".env")
        if env_file.exists():
            try:
                from dotenv import dotenv_values
                env_vals = dotenv_values(str(env_file))
                for key in _GITHUB_TOKEN_ENV_KEYS:
                    if env_vals.get(key):
                        return str(env_vals[key]).strip()
            except Exception:
                pass
        return None

    def _redact(self, text: str) -> str:
        if not text:
            return ""
        scrubbed = text
        if self.token:
            scrubbed = scrubbed.replace(self.token, "[REDACTED_TOKEN]")
            auth_b64 = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            scrubbed = scrubbed.replace(auth_b64, "[REDACTED_AUTH]")
        return scrubbed

    def _github_headers(self, json_body: bool = False) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "snowflake-semantic-sync-bot",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _run_git(
        self,
        args: List[str],
        cwd: Optional[Path] = None,
        custom_env: Optional[Dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        if custom_env:
            env.update(custom_env)

        if self.token:
            auth_b64 = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env.update({
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth_b64}",
                "GIT_CONFIG_KEY_1": "credential.helper",
                "GIT_CONFIG_VALUE_1": "",
                "GIT_TERMINAL_PROMPT": "0",
            })

        res = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            redacted_cmd = " ".join(self._redact(a) for a in args)
            redacted_err = self._redact(res.stderr or res.stdout)
            raise RuntimeError(f"Git command failed ({redacted_cmd}):\n{redacted_err}")

        return res

    def _is_repo_empty(self) -> bool:
        """Returns True if the target GitHub repository has zero branches (uninitialized repo)."""
        api_url = f"https://api.github.com/repos/{self.repo_slug}/branches?per_page=1"
        req = urllib.request.Request(api_url, headers=self._github_headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                branches = json.loads(resp.read().decode("utf-8"))
                return isinstance(branches, list) and len(branches) == 0
        except urllib.error.HTTPError as e:
            if e.code == 409:  # Git Repository is empty
                return True
            err_body = self._redact(e.read().decode("utf-8", errors="replace"))
            raise RuntimeError(f"GitHub API branch inspection failed (HTTP {e.code}): {err_body}") from None

    def _find_open_sync_pr(self, base_branch: str, branch_prefix: str) -> Optional[Dict[str, Any]]:
        if not self.token:
            return None
        query = urllib.parse.urlencode({"state": "open", "base": base_branch, "per_page": 50})
        api_url = f"https://api.github.com/repos/{self.repo_slug}/pulls?{query}"
        req = urllib.request.Request(api_url, headers=self._github_headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                pulls = json.loads(resp.read().decode("utf-8"))
                if isinstance(pulls, list):
                    for pr in pulls:
                        head_ref = (pr.get("head") or {}).get("ref", "")
                        if head_ref.startswith(branch_prefix):
                            return pr
        except Exception:
            pass
        return None

    def _configure_git_identity(self, cwd: Path) -> None:
        git_user = os.environ.get("GIT_AUTHOR_NAME", "Snowflake Semantic Sync Bot")
        git_email = os.environ.get("GIT_AUTHOR_EMAIL", "snowflake-semantic-sync-bot@users.noreply.github.com")
        self._run_git(["git", "config", "user.name", git_user], cwd=cwd)
        self._run_git(["git", "config", "user.email", git_email], cwd=cwd)

    def _bootstrap_empty_repo(self, temp_dir: Path, repo_url: str, base_branch: str) -> None:
        """Initializes an empty GitHub repository with a seed base branch so PRs can target it."""
        self._run_git(["git", "init", "-b", base_branch], cwd=temp_dir)
        self._run_git(["git", "remote", "add", "origin", repo_url], cwd=temp_dir)
        self._configure_git_identity(temp_dir)
        readme = temp_dir / "README.md"
        readme.write_text(
            f"# {self.repo_slug}\n\n"
            f"Automated LookML Analytic Model (`sql_analytic_model_name`) repository synced from "
            f"Snowflake Semantic Views by `snowflake-semantic-sync`.\n",
            encoding="utf-8",
        )
        self._run_git(["git", "add", "README.md"], cwd=temp_dir)
        self._run_git(
            ["git", "commit", "-m", "chore: initialize repository for snowflake-semantic-sync [skip ci]"],
            cwd=temp_dir,
        )
        self._run_git(["git", "push", "-u", "origin", base_branch], cwd=temp_dir)

    def _build_default_pr_body(self, lookml_files: Dict[str, str], diff_stat: str) -> str:
        file_bullets = "\n".join(f"- `{p}`" for p in sorted(lookml_files.keys()))
        return (
            "### Snowflake Semantic View → LookML (`sql_analytic_model_name`) Synchronization\n\n"
            "Automated PR generated from Snowflake Semantic Views (`SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW`).\n\n"
            "Strictly scoped to machine-managed Analytic Model base files "
            "(human-owned `views/curated/` refinements and `models/` files are never modified).\n\n"
            "#### Managed Files\n"
            f"{file_bullets}\n\n"
            "#### Diff Summary\n"
            f"```text\n{diff_stat.strip()}\n```\n"
        )

    def create_pull_request(
        self,
        lookml_files: Dict[str, str],
        base_branch: str = "main",
        branch_prefix: str = "snowflake-semantic-sync/update",
        title: Optional[str] = None,
        body: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Clones (or bootstraps), stages machine-managed LookML files, commits, pushes, and opens/updates a PR."""
        if not self.token:
            raise ValueError(
                "GitHub token not found. Please set GITHUB_TOKEN, GITHUB_PAT, or GITHUB_PERSONAL_ACCESS_TOKEN."
            )

        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
        repo_is_empty = self._is_repo_empty()
        existing_pr = (
            None
            if repo_is_empty
            else self._find_open_sync_pr(base_branch=base_branch, branch_prefix=branch_prefix)
        )
        branch_name = existing_pr["head"]["ref"] if existing_pr else f"{branch_prefix}-{timestamp}"
        temp_dir = Path(tempfile.mkdtemp(prefix="sf_git_sync_"))

        try:
            repo_url = f"https://github.com/{self.repo_slug}.git"

            if repo_is_empty:
                self._bootstrap_empty_repo(temp_dir=temp_dir, repo_url=repo_url, base_branch=base_branch)
            else:
                self._run_git(["git", "clone", "--depth=1", "--branch", base_branch, repo_url, str(temp_dir)])
                self._configure_git_identity(temp_dir)

            self._run_git(["git", "checkout", "-b", branch_name], cwd=temp_dir)

            for rel_path, content in lookml_files.items():
                target_file = temp_dir / rel_path
                target_file.parent.mkdir(parents=True, exist_ok=True)
                existing = target_file.read_text(encoding="utf-8") if target_file.exists() else ""
                if existing != content:
                    target_file.write_text(content, encoding="utf-8")

            for rel_path in lookml_files.keys():
                target_file = temp_dir / rel_path
                if target_file.exists():
                    self._run_git(["git", "add", rel_path], cwd=temp_dir)

            diff_stat_res = self._run_git(["git", "diff", "--cached", "--stat"], cwd=temp_dir)
            diff_cached_res = self._run_git(["git", "diff", "--cached"], cwd=temp_dir)

            if not diff_stat_res.stdout.strip():
                return {
                    "pr_created": False,
                    "pr_updated": False,
                    "message": "No LookML changes detected. Repository is up-to-date.",
                    "diff": "",
                }

            commit_msg = (
                f"feat(lookml): sync snowflake semantic views [skip ci]\n\n"
                f"Automated Snowflake Semantic View sync: {timestamp}"
            )
            self._run_git(["git", "commit", "-m", commit_msg], cwd=temp_dir)

            # If an open sync PR already exists, check whether its remote branch already matches HEAD
            if existing_pr:
                self._run_git(
                    ["git", "fetch", "--depth=1", "origin", f"{branch_name}:refs/remotes/origin/{branch_name}"],
                    cwd=temp_dir,
                )
                pr_delta = self._run_git(
                    ["git", "diff", "--stat", f"origin/{branch_name}", "HEAD"],
                    cwd=temp_dir,
                )
                if not pr_delta.stdout.strip():
                    pr_url = existing_pr.get("html_url", "")
                    return {
                        "pr_created": False,
                        "pr_updated": False,
                        "pr_url": pr_url,
                        "branch": branch_name,
                        "diff": "",
                        "message": f"Open Pull Request is already up-to-date: {pr_url}",
                    }

            push_cmd = (
                ["git", "push", "--force", "-u", "origin", branch_name]
                if existing_pr
                else ["git", "push", "-u", "origin", branch_name]
            )
            self._run_git(push_cmd, cwd=temp_dir)

            pr_title = title or f"Snowflake Semantic View Sync - {timestamp}"
            pr_body = body or self._build_default_pr_body(lookml_files, diff_stat_res.stdout)

            if existing_pr:
                pr_number = existing_pr["number"]
                patch_url = f"https://api.github.com/repos/{self.repo_slug}/pulls/{pr_number}"
                patch_payload = json.dumps({"title": pr_title, "body": pr_body}).encode("utf-8")
                patch_req = urllib.request.Request(
                    patch_url,
                    data=patch_payload,
                    headers=self._github_headers(json_body=True),
                    method="PATCH",
                )
                try:
                    with urllib.request.urlopen(patch_req, timeout=30) as resp:
                        resp_data = json.loads(resp.read().decode("utf-8"))
                except urllib.error.HTTPError as e:
                    err_body = self._redact(e.read().decode("utf-8", errors="replace"))
                    raise RuntimeError(f"GitHub API Pull Request update failed (HTTP {e.code}): {err_body}") from None

                pr_url = resp_data.get("html_url", existing_pr.get("html_url", ""))
                return {
                    "pr_created": False,
                    "pr_updated": True,
                    "pr_url": pr_url,
                    "branch": branch_name,
                    "diff": diff_cached_res.stdout,
                    "message": f"Existing Pull Request updated: {pr_url}",
                }

            api_url = f"https://api.github.com/repos/{self.repo_slug}/pulls"
            payload = json.dumps({
                "title": pr_title,
                "head": branch_name,
                "base": base_branch,
                "body": pr_body,
            }).encode("utf-8")
            req = urllib.request.Request(
                api_url,
                data=payload,
                headers=self._github_headers(json_body=True),
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                err_body = self._redact(e.read().decode("utf-8", errors="replace"))
                raise RuntimeError(f"GitHub API Pull Request creation failed (HTTP {e.code}): {err_body}") from None

            pr_url = resp_data.get("html_url", "")
            return {
                "pr_created": True,
                "pr_updated": False,
                "pr_url": pr_url,
                "branch": branch_name,
                "diff": diff_cached_res.stdout,
                "message": f"Pull Request successfully opened: {pr_url}",
            }
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
