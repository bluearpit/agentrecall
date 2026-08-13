from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dotagents.cli import app
from dotagents.layout import Layout
from dotagents.permissions import (
    PermissionPolicy,
    compile_native,
    init_policy,
    parse_policy,
    sync_permissions,
)

runner = CliRunner()


def test_compile_shell_and_fetch() -> None:
    policy = PermissionPolicy(
        allow_shell=("git status", "ls"),
        deny_shell=("git push --force",),
        allow_fetch=("https://github.com",),
        workspace_write=True,
    )
    rules = compile_native(policy)
    assert "Bash(git status:*)" in rules.claude_allow
    assert "Bash(ls:*)" in rules.claude_allow
    assert "WebFetch(domain:github.com)" in rules.claude_allow
    assert "Edit" in rules.claude_allow
    assert "Bash(git push --force:*)" in rules.claude_deny
    assert "Shell(git status)" in rules.cursor_allow
    assert "prefix_rule(" in rules.codex_rules
    assert '"git", "status"' in rules.codex_rules
    assert 'decision = "forbidden"' in rules.codex_rules
    assert rules.opencode_bash["git status *"] == "allow"
    assert rules.opencode_edit == "allow"


def test_parse_rejects_bad_lists() -> None:
    with pytest.raises(ValueError, match="allow_shell"):
        parse_policy({"allow_shell": "git status"})


def test_apply_merges_without_clobbering(home: Path, layout: Layout) -> None:
    init_policy(layout.permissions_file, dry_run=False)
    claude_settings = home / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True)
    claude_settings.write_text(
        json.dumps(
            {
                "model": "keep-me",
                "permissions": {"allow": ["Bash(uv run:*)"], "deny": []},
            }
        ),
        encoding="utf-8",
    )

    lines, failed = sync_permissions(layout, cwd=None, dry_run=False)
    assert failed == 0
    data = json.loads(claude_settings.read_text(encoding="utf-8"))
    assert data["model"] == "keep-me"
    assert "Bash(uv run:*)" in data["permissions"]["allow"]
    assert "Bash(git status:*)" in data["permissions"]["allow"]
    assert "Bash(git push --force:*)" in data["permissions"]["deny"]
    assert any("wrote" in line for line in lines)

    codex_rules = home / ".codex" / "rules" / "dotagents.rules"
    assert codex_rules.is_file()
    assert "dotagents" in codex_rules.read_text(encoding="utf-8")

    # Re-apply replaces managed rules instead of duplicating them.
    sync_permissions(layout, cwd=None, dry_run=False)
    data = json.loads(claude_settings.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"].count("Bash(git status:*)") == 1


def test_project_policy_stays_in_repo(home: Path, layout: Layout, tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".git").mkdir()
    init_policy(project / ".agents" / "permissions.yaml", dry_run=False)
    user_settings = home / ".claude" / "settings.json"

    _, failed = sync_permissions(layout, cwd=project, dry_run=False)
    assert failed == 0
    assert not user_settings.exists()
    project_settings = project / ".claude" / "settings.json"
    data = json.loads(project_settings.read_text(encoding="utf-8"))
    assert "Bash(git status:*)" in data["permissions"]["allow"]


def test_permissions_cli_init_and_apply(home: Path, layout: Layout) -> None:
    missing = runner.invoke(app, ["permissions"])
    assert missing.exit_code == 1
    assert "no permissions.yaml" in missing.stdout

    init = runner.invoke(app, ["permissions", "init", "--apply"])
    assert init.exit_code == 0
    assert layout.permissions_file.is_file()

    preview = runner.invoke(app, ["permissions"])
    assert preview.exit_code == 0
    assert "dry-run" in preview.stdout
    assert "Bash(git status:*)" in preview.stdout

    applied = runner.invoke(app, ["permissions", "--apply"])
    assert applied.exit_code == 0
    settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "Bash(git status:*)" in settings["permissions"]["allow"]
    assert "Bash(git push --force:*)" in settings["permissions"]["deny"]
