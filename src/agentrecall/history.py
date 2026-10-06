"""Index and query the cross-agent history catalog."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from agentrecall.history_models import (
    CommandRecord,
    SearchHit,
    SessionRecord,
    Turn,
    WorktreeSummary,
)
from agentrecall.history_parsing import (
    classify_command_kind,
    format_turn,
    iter_transcript_turns,
    load_turns,
    parse_transcript,
    select_turns,
)
from agentrecall.history_sources import (
    ProjectScope,
    agent_from_source_path,
    belongs_to_project,
    checkout_name,
    decode_encoded_project,
    discover_transcripts,
    encode_claude_project,
    encode_cursor_project,
    git_head_branch,
    git_repo_root,
    git_toplevel,
    infer_cwd_from_source_path,
    project_match_keys,
    project_scope,
    registered_worktrees,
)
from agentrecall.history_store import (
    _index_state,
    _write_records,
    connect,
    list_session_locations,
    list_stored_commands,
    list_stored_sessions,
    search_candidates,
    upsert,
)
from agentrecall.layout import Layout

__all__ = (
    "CommandRecord",
    "ProjectScope",
    "SearchHit",
    "SessionRecord",
    "Turn",
    "WorktreeSummary",
    "agent_from_source_path",
    "belongs_to_project",
    "checkout_name",
    "classify_command_kind",
    "connect",
    "decode_encoded_project",
    "discover_transcripts",
    "encode_claude_project",
    "encode_cursor_project",
    "format_turn",
    "git_head_branch",
    "git_repo_root",
    "git_toplevel",
    "infer_cwd_from_source_path",
    "iter_transcript_turns",
    "list_commands",
    "list_sessions",
    "list_worktrees",
    "load_turns",
    "parse_history_bound",
    "parse_transcript",
    "project_match_keys",
    "project_scope",
    "registered_worktrees",
    "reindex",
    "search",
    "select_turns",
    "upsert",
)

SNIPPET_RADIUS = 80
COMMAND_KINDS = ("test", "http", "git", "python", "docker", "other")
SEARCH_SORTS = ("relevance", "recent")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def parse_history_bound(raw: str, *, end_of_day: bool) -> datetime:
    text = raw.strip()
    if not text:
        raise ValueError("empty timestamp bound")
    if _ISO_DATE.fullmatch(text):
        parsed = datetime.fromisoformat(text).replace(tzinfo=UTC)
        if end_of_day:
            return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
        return parsed
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid timestamp {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _in_time_range(
    value: str | None,
    *,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    if since is None and until is None:
        return True
    parsed = None
    if value is not None:
        try:
            parsed = parse_history_bound(value, end_of_day=False)
        except ValueError:
            parsed = None
    if parsed is None:
        return True
    if since is not None and parsed < since:
        return False
    if until is not None and parsed > until:
        return False
    return True


def reindex(
    layout: Layout,
    *,
    cwd: Path | None = None,
    all_projects: bool = False,
) -> tuple[int, int]:
    stale_schema, known = _index_state(layout.history_db)
    records: list[SessionRecord] = []
    observed: set[str] = set()
    skipped = 0
    # A schema change re-parses every transcript anyway, so write all of them:
    # otherwise only the first project queried after an upgrade gets the new
    # columns and every other project keeps stale rows forever.
    scope = _scope(cwd, all_projects=all_projects or stale_schema)
    for agent, path in discover_transcripts(layout):
        source = str(path)
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        observed.add(source)
        if not stale_schema and known.get(source) == mtime_ns:
            skipped += 1
            continue
        try:
            record = parse_transcript(agent, path)
        except OSError:
            observed.discard(source)
            continue
        if scope is not None and not scope.contains(record):
            skipped += 1
            continue
        records.append(record)
    deleted_paths = known.keys() - observed
    if records or deleted_paths or stale_schema:
        _write_records(
            layout.history_db,
            records,
            deleted_paths=deleted_paths,
            stale_schema=stale_schema,
        )
    return len(records), skipped


def _scope(
    cwd: Path | None, *, all_projects: bool, this_worktree: bool = False
) -> ProjectScope | None:
    if all_projects:
        return None
    return project_scope(cwd or Path.cwd(), this_worktree=this_worktree)


def _snippet(body: str, query: str) -> str:
    lowered = body.lower()
    tokens = [token.lower() for token in query.split() if token.strip()]
    needles = list(reversed(tokens)) if tokens else [query.lower()]
    index = -1
    needle_len = 0
    for needle in needles:
        found = lowered.find(needle)
        if found >= 0:
            index = found
            needle_len = len(needle)
            break
    if index < 0:
        compact = re.sub(r"\s+", " ", body).strip()
        return compact[: SNIPPET_RADIUS * 2]
    start = max(0, index - SNIPPET_RADIUS)
    end = min(len(body), index + needle_len + SNIPPET_RADIUS)
    fragment = re.sub(r"\s+", " ", body[start:end]).strip()
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(body) else ""
    return f"{prefix}{fragment}{suffix}"


def _compact_snippet(raw: str) -> str:
    return re.sub(r"\s+", " ", raw).strip()


def _choose_snippet(body: str, query: str, fts_snippet: str) -> str:
    homemade = _snippet(body, query)
    compact = _compact_snippet(fts_snippet)
    tokens = [token.lower() for token in query.split() if token.strip()]
    if not compact or not tokens:
        return homemade
    later = tokens[-1]
    if later in compact.lower():
        return compact
    return homemade


def _fetch_limit(*, cwd: Path | None, all_projects: bool, limit: int) -> int:
    if cwd is not None and not all_projects:
        return limit * 5
    return limit


def _hit_from_record(record: SessionRecord, *, snippet: str) -> SearchHit:
    return SearchHit(
        agent=record.agent,
        source_path=record.source_path,
        project_cwd=record.project_cwd,
        started_at=record.started_at,
        title=record.title,
        snippet=snippet,
        session_id=record.session_id,
        name=record.name,
        updated_at=record.updated_at,
        searchable=record.searchable,
        resumable=record.resumable,
        git_root=record.git_root,
        repo_root=record.repo_root,
        branch=record.branch,
    )


def search(
    layout: Layout,
    query: str,
    *,
    cwd: Path | None = None,
    agent: str | None = None,
    all_projects: bool = False,
    limit: int = 20,
    sort: str = "relevance",
    branch: str | None = None,
    this_worktree: bool = False,
) -> list[SearchHit]:
    if sort not in SEARCH_SORTS:
        raise ValueError(f"unknown search sort {sort!r}")
    reindex(layout, cwd=cwd, all_projects=all_projects)
    candidates = search_candidates(
        layout.history_db,
        query,
        agent=agent,
        sort=sort,
        limit=_fetch_limit(cwd=cwd, all_projects=all_projects, limit=limit),
        branch=branch,
    )

    scope = _scope(cwd, all_projects=all_projects, this_worktree=this_worktree)
    hits: list[SearchHit] = []
    for record, fts_snippet in candidates:
        if scope is not None and not scope.contains(record):
            continue
        snippet = _choose_snippet(record.body, query, fts_snippet)
        hits.append(_hit_from_record(record, snippet=snippet))
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
    since: datetime | None = None,
    until: datetime | None = None,
    branch: str | None = None,
    this_worktree: bool = False,
) -> list[SearchHit]:
    reindex(layout, cwd=cwd, all_projects=all_projects)
    records = list_stored_sessions(
        layout.history_db,
        agent=agent,
        since=since,
        until=until,
        limit=_fetch_limit(cwd=cwd, all_projects=all_projects, limit=limit),
        branch=branch,
    )
    scope = _scope(cwd, all_projects=all_projects, this_worktree=this_worktree)
    hits: list[SearchHit] = []
    for record in records:
        if scope is not None and not scope.contains(record):
            continue
        if not _in_time_range(record.updated_at or record.started_at, since=since, until=until):
            continue
        snippet = re.sub(r"\s+", " ", record.body).strip()[: SNIPPET_RADIUS * 2]
        hits.append(_hit_from_record(record, snippet=snippet))
        if len(hits) >= limit:
            break
    return hits


def list_commands(
    layout: Layout,
    *,
    cwd: Path | None = None,
    agent: str | None = None,
    kind: str | None = None,
    all_projects: bool = False,
    limit: int = 50,
    since: datetime | None = None,
    until: datetime | None = None,
    branch: str | None = None,
    this_worktree: bool = False,
) -> list[CommandRecord]:
    if kind is not None and kind not in COMMAND_KINDS:
        raise ValueError(f"unknown command kind {kind!r}")
    reindex(layout, cwd=cwd, all_projects=all_projects)
    records = list_stored_commands(
        layout.history_db,
        agent=agent,
        kind=kind,
        since=since,
        until=until,
        limit=_fetch_limit(cwd=cwd, all_projects=all_projects, limit=limit),
        branch=branch,
    )
    scope = _scope(cwd, all_projects=all_projects, this_worktree=this_worktree)
    hits: list[CommandRecord] = []
    for session, command in records:
        if scope is not None and not scope.contains(session):
            continue
        if not _in_time_range(command.occurred_at, since=since, until=until):
            continue
        hits.append(command)
        if len(hits) >= limit:
            break
    return hits


def _activity(record: SessionRecord) -> str:
    return record.updated_at or record.started_at or ""


def _latest_branch(records: list[SessionRecord]) -> str | None:
    """Branch of the most recently active session that recorded one."""
    for record in sorted(records, key=_activity, reverse=True):
        if record.branch:
            return record.branch
    return None


def list_worktrees(layout: Layout, *, cwd: Path | None = None) -> list[WorktreeSummary]:
    """Summarize every worktree of the repository containing ``cwd``.

    Registered worktrees with no indexed chats are listed too, so an agent can
    see where work happened without guessing from ``git worktree list``.
    """
    start = (cwd or Path.cwd()).resolve()
    toplevel = git_toplevel(start)
    if toplevel is None:
        raise ValueError(f"{start} is not inside a git repository")
    repo_root = git_repo_root(toplevel) or toplevel
    reindex(layout, cwd=start)
    scope = project_scope(start)

    checkouts: dict[Path, list[SessionRecord]] = {repo_root: []}
    for path in registered_worktrees(repo_root):
        checkouts.setdefault(path, [])
    for record in list_session_locations(layout.history_db):
        if not scope.contains(record):
            continue
        base = record.git_root or record.project_cwd
        if base is None:
            continue
        checkouts.setdefault(Path(base), []).append(record)

    summaries: list[WorktreeSummary] = []
    for path in sorted(checkouts, key=lambda item: (item != repo_root, str(item))):
        records = checkouts[path]
        exists = path.is_dir()
        branch = git_head_branch(path) if exists else None
        if branch is None:
            branch = _latest_branch(records)
        counts: dict[str, int] = {}
        for record in records:
            counts[record.agent] = counts.get(record.agent, 0) + 1
        summaries.append(
            WorktreeSummary(
                repo_root=str(repo_root),
                path=str(path),
                name=checkout_name(path, repo_root),
                branch=branch,
                sessions_by_agent=dict(sorted(counts.items())),
                last_activity=max((_activity(record) for record in records), default="") or None,
                exists=exists,
            )
        )
    return summaries
