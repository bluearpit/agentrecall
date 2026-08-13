from __future__ import annotations

from pathlib import Path

from dotagents.project import sync_project


def test_project_shim_created(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# project\n", encoding="utf-8")
    line, failed = sync_project(tmp_path, dry_run=False)
    assert failed is False
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    assert "wrote" in line


def test_project_does_not_overwrite_extra(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# project\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Claude-specific notes\n", encoding="utf-8")
    line, failed = sync_project(tmp_path, dry_run=False)
    assert failed is False
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "# Claude-specific notes\n"
    assert "skip" in line


def test_project_missing_agents(tmp_path: Path) -> None:
    line, failed = sync_project(tmp_path, dry_run=False)
    assert failed is True
    assert "no AGENTS.md" in line
