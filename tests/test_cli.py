from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from agentrecall.cli import app
from agentrecall.layout import Layout

runner = CliRunner()


def test_status_and_skills_dry_run(home: Path, layout: Layout) -> None:
    layout.agents_skills.mkdir(parents=True)
    skill = layout.agents_skills / "demo"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# demo\n", encoding="utf-8")

    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0
    assert "canonical:" in status.stdout
    assert "claude" in status.stdout
    assert "0/1 linked" in status.stdout
    assert "1 native" in status.stdout
    assert "1/1 native" not in status.stdout
    assert "demo" not in status.stdout
    assert "instructions show" in status.stdout

    verbose_status = runner.invoke(app, ["status", "--verbose"])
    assert verbose_status.exit_code == 0
    assert "demo" in verbose_status.stdout

    dry = runner.invoke(app, ["skills"])
    assert dry.exit_code == 0
    assert "dry-run" in dry.stdout

    applied = runner.invoke(app, ["skills", "--apply"])
    assert applied.exit_code == 0
    dest = home / ".claude" / "skills" / "demo"
    assert dest.is_symlink()


def test_instruction_subcommands_keep_creation_and_linking_separate(
    home: Path, layout: Layout
) -> None:
    initialized = runner.invoke(app, ["instructions", "init", "--apply"])
    assert initialized.exit_code == 0
    assert layout.agents_instructions.is_file()
    assert not (home / ".codex" / "AGENTS.md").exists()

    linked = runner.invoke(
        app,
        ["instructions", "link", "--agent", "codex", "--apply"],
    )
    assert linked.exit_code == 0
    assert (home / ".codex" / "AGENTS.md").is_symlink()
    assert not (home / ".claude" / "CLAUDE.md").exists()

    shown = runner.invoke(app, ["instructions", "show"])
    assert shown.exit_code == 0
    assert "# Global agent instructions" in shown.stdout

    unused_format = runner.invoke(app, ["instructions", "show", "--format", "cursor"])
    assert unused_format.exit_code == 2


def test_legacy_instructions_command_still_combines_actions(home: Path, layout: Layout) -> None:
    result = runner.invoke(app, ["instructions", "--apply"])

    assert result.exit_code == 0
    assert layout.agents_instructions.is_file()
    assert (home / ".claude" / "CLAUDE.md").is_symlink()
    assert (home / ".codex" / "AGENTS.md").is_symlink()
