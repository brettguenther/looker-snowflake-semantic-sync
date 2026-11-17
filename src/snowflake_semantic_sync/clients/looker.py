"""Looker Client wrapping official Looker SDK and looker-cli for LookML operations."""

import os
from pathlib import Path
import shutil
import subprocess
from typing import List, Optional, Set, Tuple

import looker_sdk
from looker_sdk import models40


class LookerClient:
    """Client for interacting with Looker using official Looker SDK or looker-cli (with --profile support)."""

    def __init__(
        self,
        cli_path: str = "looker-cli",
        profile: Optional[str] = None,
        sdk: Optional[looker_sdk.methods40.Looker40SDK] = None,
    ):
        self.cli_path_name = cli_path
        self.profile = (profile or "").strip() or None
        self._cli_path = shutil.which(cli_path)
        self._created_dirs: Set[str] = set()
        self._sdk = sdk

    @property
    def sdk(self) -> Optional[looker_sdk.methods40.Looker40SDK]:
        """Lazily initializes the official Looker SDK client when no explicit looker-cli profile is requested."""
        if self._sdk is None and not self.profile:
            has_env = bool(os.environ.get("LOOKERSDK_BASE_URL") and os.environ.get("LOOKERSDK_CLIENT_ID"))
            has_ini = Path("looker.ini").exists() or Path(os.path.expanduser("~/.looker/looker.ini")).exists()
            if has_env or has_ini:
                try:
                    self._sdk = looker_sdk.init40()
                except Exception as e:
                    print(f"Warning: Failed to initialize Looker SDK: {e}")
                    self._sdk = None
        return self._sdk

    @property
    def cli_path(self) -> str:
        if not self._cli_path:
            raise FileNotFoundError(f"looker-cli executable '{self.cli_path_name}' not found in PATH.")
        return self._cli_path

    def _cli_cmd(self, *args: str) -> List[str]:
        base = [self.cli_path]
        if self.profile:
            base.extend(["--profile", self.profile])
        base.extend(args)
        return base

    def ensure_dev_mode(self) -> None:
        """Ensures the current Looker session is in dev mode."""
        if self.sdk:
            try:
                self.sdk.update_session(models40.WriteApiSession(workspace_id="dev"))
                return
            except Exception as e:
                print(f"Warning: SDK session update failed, falling back to CLI: {e}")

        cmd = self._cli_cmd("session", "update", "dev")
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to switch Looker session to dev mode: {res.stderr or res.stdout}")

    def checkout_branch(self, project_id: str, branch: str = "master") -> None:
        """Checks out the target branch in the project dev workspace."""
        if self.sdk:
            try:
                self.sdk.update_git_branch(project_id, models40.WriteGitBranch(name=branch))
                return
            except Exception as e:
                print(f"Warning: SDK checkout branch failed, falling back to CLI: {e}")

        cmd = self._cli_cmd("project", "checkout", project_id, branch)
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 and "Already on" not in (res.stdout + res.stderr):
            raise RuntimeError(
                f"Failed to checkout branch '{branch}' in project '{project_id}': {res.stderr or res.stdout}"
            )

    def ensure_directory(self, project_id: str, dir_path: str) -> None:
        """Ensures that a directory (and all its parents) exists in the project."""
        parts = Path(dir_path).parts
        accum: List[str] = []
        for p in parts:
            accum.append(p)
            sub_dir = "/".join(accum)
            key = f"{project_id}:{sub_dir}"
            if key not in self._created_dirs:
                if self._cli_path:
                    cmd = self._cli_cmd("project", "directory", "create", project_id, sub_dir)
                    subprocess.run(cmd, capture_output=True, text=True)
                self._created_dirs.add(key)

    def create_or_update_file(self, project_id: str, file_path: str, content: str) -> None:
        """Creates or updates a file in the Looker project."""
        self.ensure_dev_mode()

        parent_dir = str(Path(file_path).parent)
        if parent_dir and parent_dir != ".":
            self.ensure_directory(project_id, parent_dir)

        cmd_create = self._cli_cmd("project", "file", "create", project_id, file_path, "-")
        res_create = subprocess.run(cmd_create, input=content, capture_output=True, text=True)
        if res_create.returncode == 0:
            return

        cmd_update = self._cli_cmd("project", "file", "update", project_id, file_path, "-")
        res_update = subprocess.run(cmd_update, input=content, capture_output=True, text=True)
        if res_update.returncode != 0:
            raise RuntimeError(
                f"Failed to write file '{file_path}' to project '{project_id}':\n"
                f"Create error: {res_create.stderr or res_create.stdout}\n"
                f"Update error: {res_update.stderr or res_update.stdout}"
            )

    def validate_project(self, project_id: str) -> Tuple[bool, str]:
        """Validates the LookML in the Looker project using Looker SDK or CLI."""
        if self.sdk:
            try:
                val_res = self.sdk.validate_project(project_id)
                errors = val_res.errors or []
                if not errors:
                    return True, "Project is valid."

                err_lines = []
                for err in errors:
                    loc = f"{err.file_path}:{err.line_number}" if err.file_path else "general"
                    err_lines.append(f"[{err.severity or 'ERROR'}] ({loc}) {err.message}")
                return False, "\n".join(err_lines)
            except Exception as e:
                print(f"Warning: SDK validate_project failed, falling back to CLI: {e}")

        cmd = self._cli_cmd("project", "validate", project_id)
        res = subprocess.run(cmd, capture_output=True, text=True)
        output = (res.stdout + "\n" + res.stderr).strip()
        is_valid = res.returncode == 0 and ("Project is valid" in output or not output)
        return is_valid, output
