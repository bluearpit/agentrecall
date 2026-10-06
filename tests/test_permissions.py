from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentrecall.cli import app
from agentrecall.layout import Layout
from agentrecall.permissions import (
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


def test_compile_external_write_history(home: Path) -> None:
    policy = PermissionPolicy(
        allow_shell=("agentrecall",),
        external_write=("~/.agents/history",),
    )
    rules = compile_native(policy)
    history = str(home / ".agents" / "history")
    assert "Bash(agentrecall:*)" in rules.claude_allow
    assert "Shell(agentrecall)" in rules.cursor_allow
    assert history in rules.claude_additional_dirs
    assert history in rules.claude_sandbox_allow_write
    assert history in rules.codex_writable_roots
    assert rules.opencode_external[history] == "allow"
    assert any("Cursor has no portable extra-root mapping" in item for item in rules.skipped)


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
    history = str(home / ".agents" / "history")
    assert history in data["permissions"]["additionalDirectories"]
    assert history in data["sandbox"]["filesystem"]["allowWrite"]
    assert any("wrote" in line for line in lines)

    codex_rules = home / ".codex" / "rules" / "agentrecall.rules"
    assert codex_rules.is_file()
    assert "agentrecall" in codex_rules.read_text(encoding="utf-8")
    config_toml = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert history in config_toml
    assert "writable_roots" in config_toml

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
    assert "Bash(agentrecall:*)" in settings["permissions"]["allow"]
    assert "Bash(git push --force:*)" in settings["permissions"]["deny"]
    history = str(home / ".agents" / "history")
    assert history in settings["permissions"]["additionalDirectories"]


def test_codex_writable_roots_merge_existing_config(home: Path, layout: Layout) -> None:
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        'notify = ["turn-ended"]\n\n[sandbox_workspace_write]\nwritable_roots = ["/tmp/notes"]\n',
        encoding="utf-8",
    )
    init_policy(layout.permissions_file, dry_run=False)
    _, failed = sync_permissions(layout, cwd=None, dry_run=False)
    assert failed == 0
    text = config.read_text(encoding="utf-8")
    assert "turn-ended" in text
    assert "/tmp/notes" in text
    assert str(home / ".agents" / "history") in text
    assert text.count("[sandbox_workspace_write]") == 1


def test_home_directory_is_not_a_project(
    home: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_policy(layout.permissions_file, dry_run=False)

    lines, failed = sync_permissions(layout, cwd=home, dry_run=True)
    assert failed == 0
    assert sum(line.startswith("policy:") for line in lines) == 1
    assert not any("/.opencode/opencode.json" in line for line in lines)
    assert any(line.startswith("skip     project policy") for line in lines)

    init_here = runner.invoke(app, ["permissions", "init", "--cwd", str(home)])
    assert init_here.exit_code == 0, init_here.output
    assert "writing the user policy instead" in init_here.stdout
    assert str(layout.permissions_file) in init_here.stdout

    monkeypatch.chdir(home)
    applied = runner.invoke(app, ["permissions", "--apply"])
    assert applied.exit_code == 0, applied.output
    assert sum(line.startswith("policy:") for line in applied.stdout.splitlines()) == 1
    assert (home / ".claude" / "settings.json").is_file()
    assert (home / ".codex" / "rules" / "agentrecall.rules").is_file()
    # The project layer would have written OpenCode's legacy global config as a project file.
    assert not (home / ".opencode").exists()
    assert (home / ".config" / "opencode" / "opencode.json").is_file()


def test_missing_project_policy_is_reported(home: Path, layout: Layout, tmp_path: Path) -> None:
    init_policy(layout.permissions_file, dry_run=False)
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    lines, failed = sync_permissions(layout, cwd=project, dry_run=True)
    assert failed == 0
    assert sum(line.startswith("policy:") for line in lines) == 1
    expected = project.resolve() / ".agents" / "permissions.yaml"
    assert f"no project policy at {expected}" in "\n".join(lines)
