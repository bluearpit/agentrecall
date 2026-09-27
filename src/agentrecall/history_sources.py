"""Discover native transcripts and match them to projects."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from agentrecall.history_models import SessionRecord
from agentrecall.layout import AgentName, Layout


def encode_claude_project(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def encode_cursor_project(path: str) -> str:
    return path.lstrip("/").replace("/", "-")


def _claude_encode_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", name)


def decode_encoded_project(
    encoded: str,
    *,
    root: Path,
    encode_name: Callable[[str], str] | None = None,
) -> Path | None:
    """Rebuild an existing path from a slash-to-hyphen (or Claude) encoding."""
    remaining = encoded.lstrip("-")
    if not remaining:
        return None
    namer = encode_name if encode_name is not None else (lambda name: name)
    current = root
    while remaining:
        matches: list[tuple[int, Path, str]] = []
        for child in _dir_children(current):
            token = namer(child.name)
            if not token:
                continue
            if remaining != token and not remaining.startswith(f"{token}-"):
                continue
            rest = remaining[len(token) :]
            if rest.startswith("-"):
                rest = rest[1:]
            matches.append((len(token), child, rest))
        if not matches:
            return None
        matches.sort(key=lambda item: item[0], reverse=True)
        _, current, remaining = matches[0]
    return current


def _dir_children(current: Path) -> list[Path]:
    if not current.is_dir():
        return []
    try:
        entries = list(current.iterdir())
    except OSError:
        return []
    children: list[Path] = []
    for entry in entries:
        try:
            if entry.is_dir():
                children.append(entry)
        except OSError:
            continue
    return children


def git_toplevel(cwd: Path) -> Path | None:
    current = cwd.resolve()
    for candidate in [current, *current.parents]:
        git_entry = candidate / ".git"
        if git_entry.exists():
            return candidate
    return None


def discover_transcripts(layout: Layout) -> list[tuple[AgentName, Path]]:
    found: list[tuple[AgentName, Path]] = []
    claude = layout.agent(AgentName.claude)
    claude_root = claude.home / "projects"
    if claude_root.is_dir():
        for path in claude_root.glob("*/*.jsonl"):
            found.append((AgentName.claude, path))

    cursor = layout.agent(AgentName.cursor)
    cursor_root = cursor.home / "projects"
    if cursor_root.is_dir():
        for path in cursor_root.glob("*/agent-transcripts/*/*.jsonl"):
            found.append((AgentName.cursor, path))

    codex = layout.agent(AgentName.codex)
    sessions_root = codex.home / "sessions"
    if sessions_root.is_dir():
        for path in sessions_root.glob("**/*.jsonl"):
            found.append((AgentName.codex, path))
    archived = codex.home / "archived_sessions"
    if archived.is_dir():
        for path in archived.glob("**/*.jsonl"):
            found.append((AgentName.codex, path))
    if layout.pi_sessions_dir.is_dir():
        for path in layout.pi_sessions_dir.glob("**/*.jsonl"):
            found.append((AgentName.pi, path))
    return found


def agent_from_source_path(path: Path) -> AgentName | None:
    parts = path.parts
    if "agent-transcripts" in parts:
        return AgentName.cursor
    posix = path.as_posix()
    if "/.cursor/" in posix or posix.startswith(".cursor/"):
        return AgentName.cursor
    if "/.claude/" in posix or posix.startswith(".claude/"):
        return AgentName.claude
    if "/.codex/" in posix or posix.startswith(".codex/"):
        return AgentName.codex
    if "/.pi/agent/sessions/" in posix or posix.startswith(".pi/agent/sessions/"):
        return AgentName.pi
    return None


def infer_cwd_from_source_path(
    agent: AgentName,
    path: Path,
    *,
    root: Path | None = None,
) -> str | None:
    walk_root = Path("/") if root is None else root
    if agent is AgentName.claude:
        encoded = path.parent.name
        if not encoded.startswith("-"):
            return None
        decoded = decode_encoded_project(
            encoded,
            root=walk_root,
            encode_name=_claude_encode_name,
        )
        return None if decoded is None else str(decoded)
    if agent is AgentName.cursor:
        parts = path.parts
        if "agent-transcripts" not in parts:
            return None
        encoded = parts[parts.index("agent-transcripts") - 1]
        decoded = decode_encoded_project(encoded, root=walk_root)
        return None if decoded is None else str(decoded)
    return None


def project_match_keys(cwd: Path) -> set[str]:
    resolved = cwd.resolve()
    keys = {
        str(resolved),
        str(cwd),
        encode_claude_project(str(resolved)),
        encode_cursor_project(str(resolved)),
    }
    toplevel = git_toplevel(resolved)
    if toplevel is not None:
        keys.add(str(toplevel))
        keys.add(encode_claude_project(str(toplevel)))
        keys.add(encode_cursor_project(str(toplevel)))
    return {key for key in keys if key}


def belongs_to_project(record: SessionRecord, cwd: Path) -> bool:
    keys = project_match_keys(cwd)
    if record.project_cwd is not None and record.project_cwd in keys:
        return True
    if record.git_root is not None and record.git_root in keys:
        return True
    source = record.source_path.replace("\\", "/")
    padded = f"/{source}/"
    for key in keys:
        if f"/{key}/" in padded:
            return True
    resolved = cwd.resolve()
    toplevel = git_toplevel(resolved) or resolved
    if record.project_cwd is not None:
        try:
            Path(record.project_cwd).resolve().relative_to(toplevel)
            return True
        except ValueError:
            pass
    return False
