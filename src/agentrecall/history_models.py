"""Shared history records.

These types do not depend on file formats or SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommandRecord:
    agent: str
    source_path: str
    occurred_at: str | None
    tool: str
    command: str
    purpose: str | None
    kind: str


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
    commands: tuple[CommandRecord, ...]
    session_id: str | None = None
    name: str | None = None
    updated_at: str | None = None
    searchable: bool = True
    resumable: bool = False


@dataclass(frozen=True, slots=True)
class SearchHit:
    agent: str
    source_path: str
    project_cwd: str | None
    started_at: str | None
    title: str
    snippet: str
    session_id: str | None = None
    name: str | None = None
    updated_at: str | None = None
    searchable: bool = True
    resumable: bool = False


@dataclass(frozen=True, slots=True)
class Turn:
    role: str
    text: str
    occurred_at: str | None = None
