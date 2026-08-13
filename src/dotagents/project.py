"""Create a project CLAUDE.md shim that imports AGENTS.md."""

from __future__ import annotations

from pathlib import Path

SHIM = "@AGENTS.md\n"


def project_status(root: Path) -> tuple[str, str]:
    agents = root / "AGENTS.md"
    claude = root / "CLAUDE.md"
    if not agents.is_file():
        return "missing-agents", "no AGENTS.md in project"
    if not claude.exists():
        return "needs-shim", "AGENTS.md present; CLAUDE.md missing"
    if claude.is_symlink():
        return "ok-symlink", f"CLAUDE.md is a symlink to {claude.readlink()}"
    text = claude.read_text(encoding="utf-8")
    stripped = text.strip()
    if stripped == "@AGENTS.md":
        return "ok-shim", "CLAUDE.md already imports AGENTS.md"
    return "has-extra", "CLAUDE.md exists with extra content; left unchanged"


def sync_project(root: Path, *, dry_run: bool) -> tuple[str, bool]:
    status, detail = project_status(root)
    claude = root / "CLAUDE.md"
    if status == "needs-shim":
        if dry_run:
            return f"would write {claude} with @AGENTS.md", False
        claude.write_text(SHIM, encoding="utf-8")
        return f"wrote    {claude}  (@AGENTS.md import)", False
    if status == "has-extra":
        return f"skip     {claude}  {detail}", False
    if status == "missing-agents":
        return f"skip     {root}  {detail}", True
    return f"skip     {claude}  {detail}", False
