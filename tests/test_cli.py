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

    dry = runner.invoke(app, ["skills"])
    assert dry.exit_code == 0
    assert "dry-run" in dry.stdout

    applied = runner.invoke(app, ["skills", "--apply"])
    assert applied.exit_code == 0
    dest = home / ".claude" / "skills" / "demo"
    assert dest.is_symlink()
