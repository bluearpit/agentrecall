from __future__ import annotations

from pathlib import Path

from agentrecall.instructions import (
    init_instructions,
    link_instructions,
    render_instructions,
    sync_instructions,
)
from agentrecall.layout import AgentName, Layout
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


def test_init_creates_only_canonical_instructions(home: Path, layout: Layout) -> None:
    lines = init_instructions(layout, dry_run=False)

    assert layout.agents_instructions.is_file()
    assert not (home / ".claude" / "CLAUDE.md").exists()
    assert not (home / ".codex" / "AGENTS.md").exists()
    assert any("wrote" in line for line in lines)


def test_link_can_target_one_agent(home: Path, layout: Layout) -> None:
    init_instructions(layout, dry_run=False)

    lines, conflicts = link_instructions(
        layout,
        dry_run=False,
        mode=LinkMode.auto,
        agents=(AgentName.codex,),
    )

    assert conflicts == 0
    assert is_linked_to(home / ".codex" / "AGENTS.md", layout.agents_instructions)
    assert not (home / ".claude" / "CLAUDE.md").exists()
    assert len(lines) == 1


def test_link_requires_canonical_instructions(layout: Layout) -> None:
    try:
        link_instructions(
            layout,
            dry_run=True,
            mode=LinkMode.auto,
            agents=(AgentName.codex,),
        )
    except FileNotFoundError as exc:
        assert "instructions init --apply" in str(exc)
    else:
        raise AssertionError("missing canonical instructions should fail")


def test_render_instructions_for_cursor(layout: Layout) -> None:
    layout.agents_home.mkdir(parents=True)
    canonical_text = "# Shared\n\nKeep this spacing exactly."
    layout.agents_instructions.write_text(canonical_text, encoding="utf-8")

    assert render_instructions(layout) == canonical_text
