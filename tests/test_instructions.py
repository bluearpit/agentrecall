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


def test_link_pi_when_installed_and_keep_conflicting_file(home: Path, layout: Layout) -> None:
    init_instructions(layout, dry_run=False)
    pi_home = home / ".pi" / "agent"
    pi_home.mkdir(parents=True)

    _, conflicts = sync_instructions(layout, dry_run=False, mode=LinkMode.auto)
    assert conflicts == 0
    assert is_linked_to(pi_home / "AGENTS.md", layout.agents_instructions)

    (pi_home / "AGENTS.md").unlink()
    (pi_home / "AGENTS.md").write_text("# pi only\n", encoding="utf-8")
    lines, conflicts = link_instructions(
        layout, dry_run=False, mode=LinkMode.auto, agents=(AgentName.pi,)
    )
    assert conflicts == 1
    assert (pi_home / "AGENTS.md").read_text(encoding="utf-8") == "# pi only\n"
    assert lines[0].startswith("conflict")


def test_sync_dry_run_without_canonical_skips_missing_pi(home: Path, layout: Layout) -> None:
    lines, _ = sync_instructions(layout, dry_run=True, mode=LinkMode.auto)

    pi_file = home / ".pi" / "agent" / "AGENTS.md"
    assert any(line.startswith("skip") and str(pi_file) in line for line in lines)
    assert not (home / ".pi").exists()

    (home / ".pi" / "agent").mkdir(parents=True)
    lines, _ = sync_instructions(layout, dry_run=True, mode=LinkMode.auto)
    assert f"would link {pi_file} -> {layout.agents_instructions}" in lines


def test_existing_pi_instructions_seed_canonical_and_link(home: Path, layout: Layout) -> None:
    pi_file = home / ".pi" / "agent" / "AGENTS.md"
    pi_file.parent.mkdir(parents=True)
    pi_file.write_text("# Pi rules\n\nUse pathlib.\n", encoding="utf-8")

    lines, conflicts = sync_instructions(layout, dry_run=False, mode=LinkMode.auto)

    assert conflicts == 0
    assert f"canonical source: {pi_file}" in lines
    assert layout.agents_instructions.read_text(encoding="utf-8") == "# Pi rules\n\nUse pathlib.\n"
    assert is_linked_to(pi_file, layout.agents_instructions)


def test_differing_pi_instructions_warn_when_seeding(home: Path, layout: Layout) -> None:
    claude_file = home / ".claude" / "CLAUDE.md"
    claude_file.parent.mkdir(parents=True)
    claude_file.write_text("# Claude\n", encoding="utf-8")
    pi_file = home / ".pi" / "agent" / "AGENTS.md"
    pi_file.parent.mkdir(parents=True)
    pi_file.write_text("# Pi\n", encoding="utf-8")

    lines = init_instructions(layout, dry_run=True)

    assert (
        lines[0] == f"canonical source: {claude_file} (warning: {pi_file} differs after normalize)"
    )


def test_agent_home_file_is_conflict_not_crash(home: Path, layout: Layout) -> None:
    init_instructions(layout, dry_run=False)
    (home / ".codex").write_text("not a dir\n", encoding="utf-8")

    lines, conflicts = link_instructions(
        layout, dry_run=False, mode=LinkMode.auto, agents=(AgentName.codex,)
    )

    assert conflicts == 1
    assert lines == [
        f"conflict {home / '.codex' / 'AGENTS.md'}  parent {home / '.codex'} is not a directory"
    ]
    assert (home / ".codex").read_text(encoding="utf-8") == "not a dir\n"
