"""SQLite schema, transactions, and persistence for the history catalog."""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import ParamSpec, TypeVar

from agentrecall.history_models import CommandRecord, SessionRecord

SCHEMA_VERSION = 5
SQLITE_BUSY_TIMEOUT_MS = 5_000
SQLITE_WRITE_ATTEMPTS = 4
SNIPPET_TOKENS = 24

_P = ParamSpec("_P")
_R = TypeVar("_R")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1_000)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA journal_mode = WAL")
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
            body TEXT NOT NULL,
            session_id TEXT,
            name TEXT,
            updated_at TEXT,
            searchable INTEGER NOT NULL DEFAULT 1,
            resumable INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute("BEGIN IMMEDIATE")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
    for name, definition in (
        ("session_id", "TEXT"),
        ("name", "TEXT"),
        ("updated_at", "TEXT"),
        ("searchable", "INTEGER NOT NULL DEFAULT 1"),
        ("resumable", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
    connection.commit()
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
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY,
            source_path TEXT NOT NULL,
            agent TEXT NOT NULL,
            occurred_at TEXT,
            tool TEXT NOT NULL,
            command TEXT NOT NULL,
            purpose TEXT,
            kind TEXT NOT NULL
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS commands_by_source ON commands (source_path)")
    connection.execute("CREATE INDEX IF NOT EXISTS commands_by_kind ON commands (kind)")
    connection.execute("CREATE INDEX IF NOT EXISTS commands_by_occurred ON commands (occurred_at)")
    connection.commit()
    return connection


def _query_connection(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{db_path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1_000,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return connection


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _session_from_row(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        agent=row["agent"],
        source_path=row["source_path"],
        mtime_ns=0,
        project_cwd=row["project_cwd"],
        git_root=row["git_root"] if "git_root" in row.keys() else None,
        started_at=row["started_at"],
        title=row["title"] if "title" in row.keys() else "",
        body=row["body"] if "body" in row.keys() else "",
        commands=(),
        session_id=row["session_id"] if "session_id" in row.keys() else None,
        name=row["name"] if "name" in row.keys() else None,
        updated_at=row["updated_at"] if "updated_at" in row.keys() else None,
        searchable=bool(row["searchable"]) if "searchable" in row.keys() else True,
        resumable=(
            bool(row["resumable"] and row["session_id"] and row["project_cwd"])
            if "resumable" in row.keys()
            else False
        ),
    )


def upsert(connection: sqlite3.Connection, record: SessionRecord) -> None:
    connection.execute(
        """
        INSERT INTO sessions (
            source_path, agent, mtime_ns, project_cwd, git_root, started_at, title, body,
            session_id, name, updated_at, searchable, resumable
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_path) DO UPDATE SET
            agent = excluded.agent,
            mtime_ns = excluded.mtime_ns,
            project_cwd = excluded.project_cwd,
            git_root = excluded.git_root,
            started_at = excluded.started_at,
            title = excluded.title,
            body = excluded.body,
            session_id = excluded.session_id,
            name = excluded.name,
            updated_at = excluded.updated_at,
            searchable = excluded.searchable,
            resumable = excluded.resumable
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
            record.session_id,
            record.name,
            record.updated_at,
            int(record.searchable),
            int(record.resumable),
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
    connection.execute("DELETE FROM commands WHERE source_path = ?", (record.source_path,))
    connection.executemany(
        """
        INSERT INTO commands (
            source_path, agent, occurred_at, tool, command, purpose, kind
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item.source_path,
                item.agent,
                item.occurred_at,
                item.tool,
                item.command,
                item.purpose,
                item.kind,
            )
            for item in record.commands
        ],
    )


def existing_mtimes(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute("SELECT source_path, mtime_ns FROM sessions")
    return {row["source_path"]: int(row["mtime_ns"]) for row in rows}


def _read_index_state(db_path: Path) -> tuple[str, bool, dict[str, int]]:
    connection = _query_connection(db_path)
    try:
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        stale_schema = _schema_version(connection) < SCHEMA_VERSION
        known = existing_mtimes(connection)
        return journal_mode, stale_schema, known
    finally:
        connection.close()


def _database_is_locked(error: sqlite3.OperationalError) -> bool:
    return "locked" in str(error).lower() or "busy" in str(error).lower()


def _sleep_before_retry(attempt: int) -> None:
    delay = 0.025 * (2**attempt) + random.uniform(0, 0.025)
    time.sleep(delay)


def _retry_on_database_lock(operation: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(operation)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        for attempt in range(SQLITE_WRITE_ATTEMPTS):
            try:
                return operation(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _database_is_locked(exc) or attempt == SQLITE_WRITE_ATTEMPTS - 1:
                    raise
                _sleep_before_retry(attempt)
        raise AssertionError("database retry loop exhausted without returning or raising")

    return wrapped


@_retry_on_database_lock
def _initialize_database(db_path: Path) -> None:
    connect(db_path).close()


def _index_state(db_path: Path) -> tuple[bool, dict[str, int]]:
    if not db_path.is_file():
        _initialize_database(db_path)
    try:
        journal_mode, stale_schema, known = _read_index_state(db_path)
    except sqlite3.OperationalError:
        # Another process may have created the file but not finished the schema.
        _initialize_database(db_path)
        _journal_mode, stale_schema, known = _read_index_state(db_path)
        return stale_schema, known
    if journal_mode != "wal":
        # Existing indexes created before WAL support are upgraded once.
        _initialize_database(db_path)
        _journal_mode, stale_schema, known = _read_index_state(db_path)
    return stale_schema, known


@_retry_on_database_lock
def _write_records(
    db_path: Path,
    records: list[SessionRecord],
    *,
    deleted_paths: set[str],
    stale_schema: bool,
) -> None:
    with closing(connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        for table in ("commands", "sessions_fts", "sessions"):
            connection.executemany(
                f"DELETE FROM {table} WHERE source_path = ?",
                ((source,) for source in deleted_paths),
            )
        for record in records:
            upsert(connection, record)
        if stale_schema:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_time_bounds(
    sql: str,
    params: list[object],
    *,
    column: str,
    since: datetime | None,
    until: datetime | None,
) -> str:
    if since is not None:
        sql += f" AND {column} >= ?"
        params.append(_stamp(since))
    if until is not None:
        sql += f" AND {column} <= ?"
        params.append(_stamp(until))
    return sql


def _session_activity_sql(table_alias: str = "") -> str:
    prefix = f"{table_alias}." if table_alias else ""
    return f"COALESCE(NULLIF({prefix}updated_at, ''), {prefix}started_at)"


def _fts_query(raw: str) -> str:
    tokens = [token.strip() for token in raw.split() if token.strip()]
    if not tokens:
        return '""'
    quoted: list[str] = []
    for token in tokens:
        cleaned = token.replace('"', "")
        if cleaned:
            quoted.append(f'"{cleaned}"')
    return " AND ".join(quoted) if quoted else '""'


def search_candidates(
    db_path: Path,
    query: str,
    *,
    agent: str | None,
    sort: str,
    limit: int,
) -> list[tuple[SessionRecord, str]]:
    sql = f"""
        SELECT
            s.*,
            bm25(sessions_fts) AS rank,
            snippet(sessions_fts, 2, '', '', '...', {SNIPPET_TOKENS}) AS fts_snippet
        FROM sessions_fts
        JOIN sessions AS s ON s.source_path = sessions_fts.source_path
        WHERE sessions_fts MATCH ?
    """
    params: list[object] = [_fts_query(query)]
    if agent is not None:
        sql += " AND s.agent = ?"
        params.append(agent)
    if sort == "recent":
        sql += f" ORDER BY {_session_activity_sql('s')} DESC LIMIT ?"
    else:
        sql += f" ORDER BY rank ASC, {_session_activity_sql('s')} DESC LIMIT ?"
    params.append(limit)
    with closing(_query_connection(db_path)) as connection:
        return [
            (_session_from_row(row), row["fts_snippet"] or "")
            for row in connection.execute(sql, params)
        ]


def list_stored_sessions(
    db_path: Path,
    *,
    agent: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> list[SessionRecord]:
    sql = "SELECT * FROM sessions WHERE 1 = 1"
    params: list[object] = []
    if agent is not None:
        sql += " AND agent = ?"
        params.append(agent)
    activity = _session_activity_sql()
    sql = _append_time_bounds(sql, params, column=activity, since=since, until=until)
    sql += f" ORDER BY {activity} DESC LIMIT ?"
    params.append(limit)
    with closing(_query_connection(db_path)) as connection:
        return [_session_from_row(row) for row in connection.execute(sql, params)]


def list_stored_commands(
    db_path: Path,
    *,
    agent: str | None,
    kind: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> list[tuple[SessionRecord, CommandRecord]]:
    sql = """
        SELECT
            c.agent, c.source_path, c.occurred_at, c.tool, c.command, c.purpose, c.kind,
            s.project_cwd, s.git_root, s.started_at, s.title, s.body
        FROM commands AS c
        JOIN sessions AS s ON s.source_path = c.source_path
        WHERE 1 = 1
    """
    params: list[object] = []
    if agent is not None:
        sql += " AND c.agent = ?"
        params.append(agent)
    if kind is not None:
        sql += " AND c.kind = ?"
        params.append(kind)
    sql = _append_time_bounds(sql, params, column="c.occurred_at", since=since, until=until)
    sql += " ORDER BY c.occurred_at DESC LIMIT ?"
    params.append(limit)
    with closing(_query_connection(db_path)) as connection:
        return [
            (
                _session_from_row(row),
                CommandRecord(
                    agent=row["agent"],
                    source_path=row["source_path"],
                    occurred_at=row["occurred_at"] or row["started_at"],
                    tool=row["tool"],
                    command=row["command"],
                    purpose=row["purpose"],
                    kind=row["kind"],
                ),
            )
            for row in connection.execute(sql, params)
        ]
