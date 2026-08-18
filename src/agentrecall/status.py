"""Human-readable status of canonical files vs agent homes."""

from __future__ import annotations

from pathlib import Path

from agentrecall.layout import AgentName, Layout
from agentrecall.linking import dirs_identical, is_linked_to, resolve_link_target
from agentrecall.skills import extra_claude_skills, skill_dirs


def skill_state(canonical: Path, dest: Path) -> str:
    if is_linked_to(dest, canonical):
        return "linked"
    if dest.is_symlink():
        target = resolve_link_target(dest)
        return f"symlink->{target}"
    if dest.is_dir():
        if dirs_identical(canonical, dest):
            return "copy-identical"
        return "copy-stale"
    if dest.exists():
        return "blocked"
    return "missing"


def status_lines(layout: Layout) -> list[str]:
    lines = [
        f"canonical: {layout.agents_home}",
        f"  AGENTS.md: {_file_state(layout.agents_instructions)}",
        f"  skills:    {layout.agents_skills} ({len(skill_dirs(layout.agents_skills))} skills)",
        f"  history:   {layout.history_db} "
        f"({'present' if layout.history_db.exists() else 'missing'})",
        f"  permissions.yaml: {_file_state(layout.permissions_file)}",
        "",
        "agents:",
    ]
    for spec in layout.agents():
        installed = "yes" if layout.is_installed(spec) else "no"
        skills_note = "native" if spec.reads_agents_skills_natively else "needs-link"
        if spec.instruction_file is None:
            instructions = "user rules in the product UI (not a file)"
        else:
            instructions = _file_state(spec.instruction_file)
        lines.append(
            f"  {spec.name.value:<9} installed={installed:<3}  skills={skills_note:<10}  "
            f"instructions={instructions}"
        )

    lines.extend(["", "skills:"])
    claude = layout.agent(AgentName.claude)
    names = skill_dirs(layout.agents_skills)
    if not names:
        lines.append("  (none in ~/.agents/skills)")
    for skill in names:
        dest = claude.skills_dir / skill.name
        lines.append(f"  {skill.name:<28} agents=yes  claude={skill_state(skill, dest)}")
    for extra in extra_claude_skills(layout):
        target = resolve_link_target(extra)
        extra_note = f"symlink->{target}" if target is not None else "local"
        lines.append(f"  {extra.name:<28} agents=no   claude={extra_note}")
    return lines


def _file_state(path: Path) -> str:
    if path.is_symlink():
        return f"symlink -> {path.readlink()}"
    if path.is_file():
        return "file"
    if path.exists():
        return "exists"
    return "missing"
