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


def _read_pointer_file(path: Path, prefix: str) -> Path | None:
    """Read a one-line git pointer file such as ``gitdir: <path>``."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if prefix:
        if not text.startswith(prefix):
            return None
        text = text[len(prefix) :].strip()
    if not text:
        return None
    target = Path(text)
    if not target.is_absolute():
        target = path.parent / target
    return target


def git_dir(toplevel: Path) -> Path | None:
    """Return the git directory for a checkout: ``.git`` or the linked worktree's gitdir."""
    entry = toplevel / ".git"
    if entry.is_dir():
        return entry
    if entry.is_file():
        target = _read_pointer_file(entry, "gitdir:")
        if target is not None and target.is_dir():
            return target.resolve()
    return None


def is_linked_worktree(toplevel: Path) -> bool:
    return (toplevel / ".git").is_file()


def git_repo_root(toplevel: Path) -> Path | None:
    """Return the main checkout shared by every worktree of ``toplevel``.

    A plain checkout is its own repo root. A linked worktree keeps a ``.git``
    file pointing at ``<repo>/.git/worktrees/<name>``; that directory's
    ``commondir`` file leads back to the shared ``<repo>/.git``. No git
    subprocess is needed. Returns None when the pointer cannot be followed.
    """
    directory = git_dir(toplevel)
    if directory is None:
        return None
    if not is_linked_worktree(toplevel):
        return toplevel
    common = _read_pointer_file(directory / "commondir", "")
    if common is not None and common.is_dir():
        common = common.resolve()
    elif directory.parent.name == "worktrees" and directory.parent.parent.name == ".git":
        common = directory.parent.parent
    else:
        return None
    return common.parent if common.name == ".git" else common


def git_head_branch(toplevel: Path) -> str | None:
    """Return the branch named in HEAD, or None for a detached or unreadable HEAD."""
    directory = git_dir(toplevel)
    if directory is None:
        return None
    try:
        head = (directory / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    if head.startswith(prefix) and len(head) > len(prefix):
        return head[len(prefix) :]
    return None


def describe_checkout(project_cwd: str | None) -> tuple[str | None, str | None, str | None]:
    """Return ``(git_root, repo_root, branch)`` for a session's project directory.

    ``branch`` is read from HEAD only for a linked worktree, where it is stable
    by construction. The main checkout switches branches too often for the
    current HEAD to describe an old session, so it stays None there and the
    transcript's own branch metadata is used instead when present.
    """
    if project_cwd is None:
        return None, None, None
    toplevel = git_toplevel(Path(project_cwd))
    if toplevel is None:
        return None, None, None
    repo_root = git_repo_root(toplevel)
    branch = git_head_branch(toplevel) if is_linked_worktree(toplevel) else None
    return str(toplevel), None if repo_root is None else str(repo_root), branch


def registered_worktrees(repo_root: Path) -> list[Path]:
    """List linked worktree checkouts registered under ``<repo_root>/.git/worktrees``."""
    worktrees_dir = repo_root / ".git" / "worktrees"
    if not worktrees_dir.is_dir():
        return []
    found: list[Path] = []
    for entry in sorted(worktrees_dir.iterdir()):
        pointer = _read_pointer_file(entry / "gitdir", "")
        if pointer is None:
            continue
        checkout = pointer.parent if pointer.name == ".git" else pointer
        if checkout.is_dir():
            found.append(checkout.resolve())
    return found


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


def _add_path_keys(keys: set[str], path: Path) -> None:
    keys.add(str(path))
    keys.add(encode_claude_project(str(path)))
    keys.add(encode_cursor_project(str(path)))


def project_match_keys(cwd: Path, *, this_worktree: bool = False) -> set[str]:
    """Keys that identify ``cwd``'s project.

    By default the keys cover the whole repository, so a query from any
    worktree matches sessions recorded in every other worktree of the same
    repo. ``this_worktree`` narrows the keys to the checkout that contains
    ``cwd``.
    """
    resolved = cwd.resolve()
    keys = {str(resolved), str(cwd)}
    _add_path_keys(keys, resolved)
    toplevel = git_toplevel(resolved)
    if toplevel is not None:
        _add_path_keys(keys, toplevel)
        repo_root = None if this_worktree else git_repo_root(toplevel)
        if repo_root is not None:
            _add_path_keys(keys, repo_root)
    return {key for key in keys if key}


def belongs_to_project(record: SessionRecord, cwd: Path, *, this_worktree: bool = False) -> bool:
    keys = project_match_keys(cwd, this_worktree=this_worktree)
    if record.project_cwd is not None and record.project_cwd in keys:
        return True
    if record.git_root is not None and record.git_root in keys:
        return True
    if not this_worktree and record.repo_root is not None and record.repo_root in keys:
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
