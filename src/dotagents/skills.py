"""Discover and link skills from ~/.agents/skills."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from dotagents.layout import AgentName, Layout
from dotagents.linking import IGNORE_NAMES, Op, PlanItem, apply_item, plan_directory_symlink


def bundled_skills_dir() -> Path:
    return Path(__file__).resolve().parent / "data" / "skills"


def skill_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in IGNORE_NAMES:
            continue
        if (child / "SKILL.md").is_file():
            found.append(child)
    return found


def bundled_install_plan(layout: Layout) -> list[PlanItem]:
    items: list[PlanItem] = []
    bundled_root = bundled_skills_dir()
    if not bundled_root.is_dir():
        return items
    for skill in skill_dirs(bundled_root):
        dest = layout.agents_skills / skill.name
        items.append(plan_directory_symlink(skill, dest))
    return items


def claude_link_plan(layout: Layout) -> list[PlanItem]:
    claude = layout.agent(AgentName.claude)
    items: list[PlanItem] = []
    names = {skill.name for skill in skill_dirs(layout.agents_skills)}
    names.update(skill.name for skill in skill_dirs(bundled_skills_dir()))
    for name in sorted(names):
        src = layout.agents_skills / name
        if not src.is_dir():
            bundled = bundled_skills_dir() / name
            if bundled.is_dir():
                src = bundled
            else:
                continue
        dest = claude.skills_dir / name
        items.append(plan_directory_symlink(src, dest))
    return items


def extra_claude_skills(layout: Layout) -> list[Path]:
    claude = layout.agent(AgentName.claude)
    canonical_names = {path.name for path in skill_dirs(layout.agents_skills)}
    canonical_names.update(path.name for path in skill_dirs(bundled_skills_dir()))
    extras: list[Path] = []
    for skill in skill_dirs(claude.skills_dir):
        if skill.name not in canonical_names:
            extras.append(skill)
    return extras


def adopt_plan(layout: Layout) -> list[PlanItem]:
    items: list[PlanItem] = []
    for extra in extra_claude_skills(layout):
        dest = layout.agents_skills / extra.name
        source = extra.resolve() if extra.is_symlink() else extra
        items.append(plan_directory_symlink(source, dest))
    return items


def sync_skills(layout: Layout, *, dry_run: bool) -> tuple[list[str], int]:
    bundled_items = bundled_install_plan(layout)
    lines, conflicts = render_and_apply(bundled_items, dry_run=dry_run)
    claude_items = claude_link_plan(layout)
    more_lines, more_conflicts = render_and_apply(claude_items, dry_run=dry_run)
    return lines + more_lines, conflicts + more_conflicts


def render_and_apply(items: Iterable[PlanItem], *, dry_run: bool) -> tuple[list[str], int]:
    lines: list[str] = []
    conflicts = 0
    for item in items:
        lines.append(apply_item(item, dry_run=dry_run))
        if item.op is Op.conflict:
            conflicts += 1
    return lines, conflicts
