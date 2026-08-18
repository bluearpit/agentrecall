from __future__ import annotations

from pathlib import Path

from agentrecall.instructions import sync_instructions
from agentrecall.layout import Layout
from agentrecall.linking import LinkMode, is_linked_to


def test_instructions_normalize_and_symlink(home: Path, layout: Layout) -> None:
    claude = home / ".claude"
    codex = home / ".codex"
    claude.mkdir()
    codex.mkdir()
    (claude / "CLAUDE.md").write_text(
        'Never include the "Generated with Claude Code" footer.\n',
        encoding="utf-8",
    )
    (codex / "AGENTS.md").write_text(
        'Never include the "Generated with Codex" footer.\n',
        encoding="utf-8",
    )

    lines, conflicts = sync_instructions(layout, dry_run=False, mode=LinkMode.auto)
    assert conflicts == 0
    canonical = layout.agents_instructions.read_text(encoding="utf-8")
    assert "Generated with an AI coding agent" in canonical
    assert "Claude Code" not in canonical
    assert is_linked_to(claude / "CLAUDE.md", layout.agents_instructions)
    assert is_linked_to(codex / "AGENTS.md", layout.agents_instructions)
    assert any("wrote" in line or "linked" in line for line in lines)


def test_instructions_hardlink(home: Path, layout: Layout) -> None:
    layout.agents_home.mkdir(parents=True)
    layout.agents_instructions.write_text("# shared\n", encoding="utf-8")
    claude = home / ".claude"
    claude.mkdir()
    (claude / "CLAUDE.md").write_text("# shared\n", encoding="utf-8")

    _, conflicts = sync_instructions(layout, dry_run=False, mode=LinkMode.hardlink)
    assert conflicts == 0
    dest = claude / "CLAUDE.md"
    src = layout.agents_instructions
    assert dest.stat().st_ino == src.stat().st_ino
    assert not dest.is_symlink()
