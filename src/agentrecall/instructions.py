"""Canonicalize global AGENTS.md and link per-agent instruction files."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from agentrecall.layout import AgentName, Layout
from agentrecall.linking import (
    LinkMode,
    Op,
    PlanItem,
    apply_item,
    is_linked_to,
    plan_file_link,
)

GENERATED_FOOTERS = (
    "Generated with Claude Code",
    "Generated with Codex",
    "Generated with Cursor",
    "Generated with OpenCode",
)
GENERIC_FOOTER = "Generated with an AI coding agent"
TITLE_LINES = (
    "# Global CLAUDE.md Instructions",
    "# Global AGENTS.md Instructions",
)
GENERIC_TITLE = "# Global agent instructions"

DEFAULT_INSTRUCTIONS = """# Global agent instructions

## Git commit messages

Never include Co-Authored-By lines in git commit messages.

## Pull requests

Never include the "Generated with an AI coding agent" footer line in PR descriptions.

Keep PR descriptions tight. A short paragraph of context, a brief bullet list of the actual \
changes, and a single line of test confirmation is enough.
"""
LINKABLE_AGENTS = (AgentName.claude, AgentName.codex)
MISSING_CANONICAL = (
    "canonical instructions are missing; run `agentrecall instructions init --apply` first"
)


class InstructionTarget(StrEnum):
    claude = "claude"
    codex = "codex"


def normalize_instructions(text: str) -> str:
    normalized = text
    for footer in GENERATED_FOOTERS:
        normalized = normalized.replace(footer, GENERIC_FOOTER)
    for title in TITLE_LINES:
        normalized = normalized.replace(title, GENERIC_TITLE)
    return normalized.rstrip() + "\n"


def _read_if_file(path: Path) -> str | None:
    if path.is_symlink():
        return None
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def resolve_canonical_text(layout: Layout) -> tuple[str, str]:
    existing = _read_if_file(layout.agents_instructions)
    if existing is not None and existing.strip():
        return normalize_instructions(existing), "existing ~/.agents/AGENTS.md"

    claude = layout.agent(AgentName.claude)
    codex = layout.agent(AgentName.codex)
    claude_text = _read_if_file(claude.instruction_file) if claude.instruction_file else None
    codex_text = _read_if_file(codex.instruction_file) if codex.instruction_file else None

    sources: list[tuple[str, str]] = []
    if claude_text is not None and claude_text.strip():
        sources.append((normalize_instructions(claude_text), str(claude.instruction_file)))
    if codex_text is not None and codex_text.strip():
        sources.append((normalize_instructions(codex_text), str(codex.instruction_file)))

    if not sources:
        return DEFAULT_INSTRUCTIONS, "built-in default"

    first_text, first_origin = sources[0]
    for text, origin in sources[1:]:
        if text != first_text:
            return first_text, f"{first_origin} (warning: {origin} differs after normalize)"
    return first_text, first_origin


def write_canonical(layout: Layout, text: str, *, dry_run: bool) -> str:
    dest = layout.agents_instructions
    if dest.exists() and not dest.is_symlink() and files_identical_text(dest, text):
        return f"skip     {dest}  canonical already up to date"
    if dry_run:
        return f"would write {dest}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return f"wrote    {dest}"


def files_identical_text(path: Path, text: str) -> bool:
    if not path.is_file():
        return False
    return path.read_text(encoding="utf-8") == text


def _canonical_path(layout: Layout) -> Path:
    if not layout.agents_instructions.is_file():
        raise FileNotFoundError(MISSING_CANONICAL)
    return layout.agents_instructions


def instruction_link_plan(
    layout: Layout,
    mode: LinkMode,
    *,
    agents: tuple[AgentName, ...],
) -> list[PlanItem]:
    src = layout.agents_instructions
    items: list[PlanItem] = []
    canonical_text = src.read_text(encoding="utf-8") if src.is_file() else ""
    for name in agents:
        spec = layout.agent(name)
        if spec.instruction_file is None:
            continue
        dest = spec.instruction_file
        if (
            dest.is_file()
            and not dest.is_symlink()
            and not is_linked_to(dest, src)
            and canonical_text
            and normalize_instructions(dest.read_text(encoding="utf-8")) == canonical_text
        ):
            op = Op.replace_with_hardlink if mode is LinkMode.hardlink else Op.replace_with_symlink
            items.append(
                PlanItem(
                    op,
                    dest,
                    src,
                    "same instructions after normalize; replace with link",
                )
            )
            continue
        items.append(plan_file_link(src, dest, mode))
    return items


def init_instructions(layout: Layout, *, dry_run: bool) -> list[str]:
    text, origin = resolve_canonical_text(layout)
    return [f"canonical source: {origin}", write_canonical(layout, text, dry_run=dry_run)]


def link_instructions(
    layout: Layout,
    *,
    dry_run: bool,
    mode: LinkMode,
    agents: tuple[AgentName, ...],
) -> tuple[list[str], int]:
    _canonical_path(layout)
    lines: list[str] = []
    conflicts = 0
    for item in instruction_link_plan(layout, mode, agents=agents):
        lines.append(apply_item(item, dry_run=dry_run))
        if item.op is Op.conflict:
            conflicts += 1
    return lines, conflicts


def render_instructions(layout: Layout) -> str:
    return _canonical_path(layout).read_text(encoding="utf-8")


def sync_instructions(layout: Layout, *, dry_run: bool, mode: LinkMode) -> tuple[list[str], int]:
    lines = init_instructions(layout, dry_run=dry_run)
    if dry_run and not layout.agents_instructions.exists():
        # Linking needs the canonical file to exist; report intended links anyway.
        for name in LINKABLE_AGENTS:
            spec = layout.agent(name)
            if spec.instruction_file is None:
                continue
            lines.append(f"would link {spec.instruction_file} -> {layout.agents_instructions}")
        return lines, 0

    link_lines, conflicts = link_instructions(
        layout,
        dry_run=dry_run,
        mode=mode,
        agents=LINKABLE_AGENTS,
    )
    return [*lines, *link_lines], conflicts
