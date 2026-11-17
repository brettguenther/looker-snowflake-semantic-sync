"""Unit tests for LookerClient (--profile, create/update fallback) and GitHubProvider helpers."""

from subprocess import CompletedProcess
from unittest.mock import patch

from snowflake_semantic_sync.clients.git_provider import GitHubProvider
from snowflake_semantic_sync.clients.looker import LookerClient


def test_looker_client_profile_cli_flags_and_update_fallback():
    calls = []

    def fake_run(cmd, input=None, capture_output=True, text=True):
        calls.append(cmd)
        # Simulate file already exists on 'create', succeed on 'update'
        if "file" in cmd and "create" in cmd:
            return CompletedProcess(cmd, 1, stdout="", stderr="409 file already exists")
        if "validate" in cmd:
            return CompletedProcess(cmd, 0, stdout="Project is valid.", stderr="")
        return CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch("shutil.which", return_value="/usr/local/bin/looker-cli"), patch("subprocess.run", side_effect=fake_run):
        client = LookerClient(profile="my-looker-profile")
        # Explicit profile must not lazily pick up ambient Looker SDK env vars
        assert client.sdk is None

        client.create_or_update_file("my_project", "views/base/demo.base.view.lkml", "view: demo {}")
        valid, msg = client.validate_project("my_project")

    assert valid is True
    assert "Project is valid" in msg
    assert any(
        cmd == ["/usr/local/bin/looker-cli", "--profile", "my-looker-profile", "project", "file", "create", "my_project", "views/base/demo.base.view.lkml", "-"]
        for cmd in calls
    )
    assert any(
        cmd == ["/usr/local/bin/looker-cli", "--profile", "my-looker-profile", "project", "file", "update", "my_project", "views/base/demo.base.view.lkml", "-"]
        for cmd in calls
    )
    assert not any("--force" in cmd for cmd in calls)


def test_github_provider_slug_normalization_and_token_resolution(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "example_test_token_12345")

    provider = GitHubProvider("https://github.com/acme-org/looker-semantic-views.git")
    assert provider.repo_slug == "acme-org/looker-semantic-views"
    assert provider.token == "example_test_token_12345"
    assert "[REDACTED_TOKEN]" in provider._redact("Authorization failed for example_test_token_12345")
