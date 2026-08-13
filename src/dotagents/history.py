"""Index and search local coding-agent transcripts without copying them."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dotagents.layout import AgentName, Layout

MAX_BODY_CHARS = 200_000
SNIPPET_RADIUS = 80

_SKIP_KEYS = frozenset(
    {
        "base_instructions",
        "permissions",
        "tool_use",
        "tool_result",
        "snapshot",
        "file-history-snapshot",
    }
)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    agent: str
    source_path: str
    mtime_ns: int
    project_cwd: str | None
    git_root: str | None
    started_at: str | None
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class SearchHit:
    agent: str
    source_path: str
    project_cwd: str | None
    started_at: str | None
    title: str
    snippet: str


def encode_claude_project(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def encode_cursor_project(path: str) -> str:
    stripped = path.lstrip("/")
    return stripped.replace("/", "-").replace("_", "-")


def git_toplevel(cwd: Path) -> Path | None:
    current = cwd.resolve()
    for candidate in [current, *current.parents]:
        git_entry = candidate / ".git"
        if git_entry.exists():
            return candidate
    return None


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            source_path TEXT PRIMARY KEY,
            agent TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL,
            project_cwd TEXT,
            git_root TEXT,
            started_at TEXT,
            title TEXT NOT NULL,
            body TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
            source_path UNINDEXED,
            title,
            body,
            project_cwd,
            tokenize = 'unicode61'
        )
        """
    )
    return connection


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
    return found


def _walk_strings(value: object, *, skip: bool = False) -> Iterator[str]:
    if skip:
        return
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            yield stripped
        return
    if isinstance(value, list):
        for item in value:
            yield from _walk_strings(item, skip=skip)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key_skip = skip or key in _SKIP_KEYS
            yield from _walk_strings(item, skip=key_skip)


def _first_user_title(texts: list[str]) -> str:
    for text in texts:
        cleaned = re.sub(r"</?user_query>", "", text).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if cleaned and not cleaned.startswith("<"):
            return cleaned[:120]
    if texts:
        return re.sub(r"\s+", " ", texts[0])[:120]
    return "(untitled)"


def parse_transcript(agent: AgentName, path: Path) -> SessionRecord:
    texts: list[str] = []
    project_cwd: str | None = None
    started_at: str | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if started_at is None:
                    timestamp = payload.get("timestamp")
                    if isinstance(timestamp, str):
                        started_at = timestamp
                extracted_cwd = _extract_cwd(agent, payload)
                if extracted_cwd is not None and project_cwd is None:
                    project_cwd = extracted_cwd
                if _is_indexable_event(agent, payload):
                    texts.extend(_walk_strings(payload))
                if sum(len(text) for text in texts) >= MAX_BODY_CHARS:
                    break
    except OSError:
        texts = []

    if project_cwd is None:
        project_cwd = infer_cwd_from_source_path(agent, path)

    git_root: str | None = None
    if project_cwd is not None:
        toplevel = git_toplevel(Path(project_cwd))
        if toplevel is not None:
            git_root = str(toplevel)

    body = "\n".join(texts)[:MAX_BODY_CHARS]
    stat = path.stat()
    if started_at is None:
        started_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
    return SessionRecord(
        agent=agent.value,
        source_path=str(path),
        mtime_ns=stat.st_mtime_ns,
        project_cwd=project_cwd,
        git_root=git_root,
        started_at=started_at,
        title=_first_user_title(texts),
        body=body,
    )


def _extract_cwd(agent: AgentName, payload: dict[str, object]) -> str | None:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.startswith("/"):
        return cwd
    nested = payload.get("payload")
    if isinstance(nested, dict):
        nested_cwd = nested.get("cwd")
        if isinstance(nested_cwd, str) and nested_cwd.startswith("/"):
            return nested_cwd
    return None


def _is_indexable_event(agent: AgentName, payload: dict[str, object]) -> bool:
    event_type = payload.get("type")
    role = payload.get("role")
    if agent is AgentName.claude:
        return event_type in {"user", "assistant"}
    if agent is AgentName.cursor:
        return role in {"user", "assistant"}
    if agent is AgentName.codex:
        if event_type == "event_msg":
            nested = payload.get("payload")
            if isinstance(nested, dict) and nested.get("type") == "user_message":
                return True
        if event_type == "response_item":
            nested = payload.get("payload")
            if isinstance(nested, dict) and nested.get("role") in {"user", "assistant"}:
                return True
        return False
    return False


def infer_cwd_from_source_path(agent: AgentName, path: Path) -> str | None:
    if agent is AgentName.claude:
        encoded = path.parent.name
        if encoded.startswith("-"):
            return encoded.replace("-", "/")
        return None
    if agent is AgentName.cursor:
        parts = path.parts
        if "agent-transcripts" not in parts:
            return None
        encoded = parts[parts.index("agent-transcripts") - 1]
        if encoded.startswith("Users-") or encoded.startswith("home-"):
            return "/" + encoded.replace("-", "/")
        return None
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


def upsert(connection: sqlite3.Connection, record: SessionRecord) -> None:
    connection.execute(
        """
        INSERT INTO sessions (
            source_path, agent, mtime_ns, project_cwd, git_root, started_at, title, body
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_path) DO UPDATE SET
            agent = excluded.agent,
            mtime_ns = excluded.mtime_ns,
            project_cwd = excluded.project_cwd,
            git_root = excluded.git_root,
            started_at = excluded.started_at,
            title = excluded.title,
            body = excluded.body
        """,
        (
            record.source_path,
            record.agent,
            record.mtime_ns,
            record.project_cwd,
            record.git_root,
            record.started_at,
            record.title,
            record.body,
        ),
    )
    connection.execute("DELETE FROM sessions_fts WHERE source_path = ?", (record.source_path,))
    connection.execute(
        """
        INSERT INTO sessions_fts (source_path, title, body, project_cwd)
        VALUES (?, ?, ?, ?)
        """,
        (record.source_path, record.title, record.body, record.project_cwd or ""),
    )


def existing_mtimes(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute("SELECT source_path, mtime_ns FROM sessions")
    return {row["source_path"]: int(row["mtime_ns"]) for row in rows}


def reindex(
    layout: Layout,
    *,
    cwd: Path | None = None,
    all_projects: bool = False,
) -> tuple[int, int]:
    connection = connect(layout.history_db)
    known = existing_mtimes(connection)
    indexed = 0
    skipped = 0
    scope = None if all_projects else (cwd or Path.cwd())
    for agent, path in discover_transcripts(layout):
        source = str(path)
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        if known.get(source) == mtime_ns:
            skipped += 1
            continue
        record = parse_transcript(agent, path)
        if scope is not None and not belongs_to_project(record, scope):
            skipped += 1
            continue
        upsert(connection, record)
        indexed += 1
    connection.commit()
    connection.close()
    return indexed, skipped


def _fts_query(raw: str) -> str:
    tokens = [token.strip() for token in raw.split() if token.strip()]
    if not tokens:
        return '""'
    quoted: list[str] = []
    for token in tokens:
        cleaned = token.replace('"', "")
        if not cleaned:
            continue
        quoted.append(f'"{cleaned}"')
    if not quoted:
        return '""'
    return " AND ".join(quoted)


def _snippet(body: str, query: str) -> str:
    lowered = body.lower()
    needle = query.split()[0].lower() if query.split() else query.lower()
    index = lowered.find(needle)
    if index < 0:
        compact = re.sub(r"\s+", " ", body).strip()
        return compact[: SNIPPET_RADIUS * 2]
    start = max(0, index - SNIPPET_RADIUS)
    end = min(len(body), index + len(needle) + SNIPPET_RADIUS)
    fragment = re.sub(r"\s+", " ", body[start:end]).strip()
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(body) else ""
    return f"{prefix}{fragment}{suffix}"


def search(
    layout: Layout,
    query: str,
    *,
    cwd: Path | None = None,
    agent: str | None = None,
    all_projects: bool = False,
    limit: int = 20,
) -> list[SearchHit]:
    reindex(layout, cwd=cwd, all_projects=all_projects)
    connection = connect(layout.history_db)
    sql = """
        SELECT s.agent, s.source_path, s.project_cwd, s.started_at, s.title, s.body
        FROM sessions_fts
        JOIN sessions AS s ON s.source_path = sessions_fts.source_path
        WHERE sessions_fts MATCH ?
    """
    params: list[object] = [_fts_query(query)]
    if agent is not None:
        sql += " AND s.agent = ?"
        params.append(agent)
    sql += " ORDER BY s.started_at DESC LIMIT ?"
    params.append(limit * 5 if cwd is not None and not all_projects else limit)
    rows = list(connection.execute(sql, params))
    connection.close()

    scope = None if all_projects else (cwd or Path.cwd())
    hits: list[SearchHit] = []
    for row in rows:
        record = SessionRecord(
            agent=row["agent"],
            source_path=row["source_path"],
            mtime_ns=0,
            project_cwd=row["project_cwd"],
            git_root=None,
            started_at=row["started_at"],
            title=row["title"],
            body=row["body"],
        )
        if scope is not None and not belongs_to_project(record, scope):
            continue
        hits.append(
            SearchHit(
                agent=row["agent"],
                source_path=row["source_path"],
                project_cwd=row["project_cwd"],
                started_at=row["started_at"],
                title=row["title"],
                snippet=_snippet(row["body"], query),
            )
        )
        if len(hits) >= limit:
            break
    return hits


def list_sessions(
    layout: Layout,
    *,
    cwd: Path | None = None,
    agent: str | None = None,
    all_projects: bool = False,
    limit: int = 20,
) -> list[SearchHit]:
    reindex(layout, cwd=cwd, all_projects=all_projects)
    connection = connect(layout.history_db)
    sql = """
        SELECT agent, source_path, project_cwd, started_at, title, body
        FROM sessions
        WHERE 1 = 1
    """
    params: list[object] = []
    if agent is not None:
        sql += " AND agent = ?"
        params.append(agent)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit * 5 if cwd is not None and not all_projects else limit)
    rows = list(connection.execute(sql, params))
    connection.close()
    scope = None if all_projects else (cwd or Path.cwd())
    hits: list[SearchHit] = []
    for row in rows:
        record = SessionRecord(
            agent=row["agent"],
            source_path=row["source_path"],
            mtime_ns=0,
            project_cwd=row["project_cwd"],
            git_root=None,
            started_at=row["started_at"],
            title=row["title"],
            body=row["body"],
        )
        if scope is not None and not belongs_to_project(record, scope):
            continue
        hits.append(
            SearchHit(
                agent=row["agent"],
                source_path=row["source_path"],
                project_cwd=row["project_cwd"],
                started_at=row["started_at"],
                title=row["title"],
                snippet=re.sub(r"\s+", " ", row["body"]).strip()[: SNIPPET_RADIUS * 2],
            )
        )
        if len(hits) >= limit:
            break
    return hits
