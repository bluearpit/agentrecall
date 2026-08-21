"""Index and search local coding-agent transcripts without copying them."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agentrecall.layout import AgentName, Layout

MAX_BODY_CHARS = 200_000
MAX_COMMAND_CHARS = 8_000
MAX_COMMANDS_PER_SESSION = 1_000
MAX_TURN_CHARS = 8_000
SNIPPET_RADIUS = 80
SNIPPET_TOKENS = 24
SCHEMA_VERSION = 3
COMMAND_KINDS = ("test", "http", "git", "python", "docker", "other")
SEARCH_SORTS = ("relevance", "recent")

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
_SHELL_TOOL_NAMES = frozenset({"Bash", "Shell", "bash"})
_CODEX_EXEC_NAMES = frozenset({"exec_command", "exec"})
_CODEX_CMD_DOUBLE = re.compile(r'\bcmd:\s*"((?:\\.|[^"\\])*)"')
_CODEX_CMD_SINGLE = re.compile(r"\bcmd:\s*'((?:\\.|[^'\\])*)'")
_TEST_MARKERS = (
    "pytest",
    "hatch test",
    "cargo test",
    "go test",
    "npm test",
    "pnpm test",
    "yarn test",
    "npx playwright",
    "playwright test",
    "python -m unittest",
    "python3 -m unittest",
    "python -m pytest",
    "python3 -m pytest",
    "uv run pytest",
)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


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


@dataclass(frozen=True, slots=True)
class SearchHit:
    agent: str
    source_path: str
    project_cwd: str | None
    started_at: str | None
    title: str
    snippet: str


@dataclass(frozen=True, slots=True)
class Turn:
    role: str
    text: str
    occurred_at: str | None = None


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


def classify_command_kind(command: str) -> str:
    lowered = re.sub(r"\s+", " ", command).strip().lower()
    if any(marker in lowered for marker in _TEST_MARKERS):
        return "test"
    if re.search(r"\b(curl|wget|httpie)\b", lowered):
        return "http"
    if re.search(r"\bgit\b", lowered):
        return "git"
    if re.search(r"\b(docker-compose|docker|kubectl|kind|helm)\b", lowered):
        return "docker"
    if re.search(r"\b(python3?|uv run |hatch run )\b", lowered):
        return "python"
    return "other"


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
    connection.execute(
        "CREATE INDEX IF NOT EXISTS commands_by_source ON commands (source_path)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS commands_by_kind ON commands (kind)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS commands_by_occurred ON commands (occurred_at)"
    )
    return connection


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _bump_schema_if_needed(connection: sqlite3.Connection) -> bool:
    if _schema_version(connection) >= SCHEMA_VERSION:
        return False
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return True


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


def _normalize_timestamp(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _unescape_cmd(raw: str) -> str:
    return raw.replace(r"\"", '"').replace(r"\'", "'").replace(r"\\", "\\")


def _content_parts(payload: dict[str, object]) -> Iterator[dict[str, object]]:
    for blob in (payload.get("message"), payload):
        if not isinstance(blob, dict):
            continue
        content = blob.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict):
                yield part


def _purpose_from_mapping(mapping: dict[str, object]) -> str | None:
    for key in ("description", "justification", "purpose"):
        value = mapping.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped[:200]
    return None


def _parsed_command(
    *,
    occurred_at: str | None,
    tool: str,
    command: str,
    purpose: str | None,
) -> CommandRecord | None:
    stripped = command.strip()
    if not stripped:
        return None
    clipped = stripped[:MAX_COMMAND_CHARS]
    return CommandRecord(
        agent="",
        source_path="",
        occurred_at=_normalize_timestamp(occurred_at),
        tool=tool,
        command=clipped,
        purpose=purpose,
        kind=classify_command_kind(clipped),
    )


def _commands_from_tool_use(
    payload: dict[str, object],
    *,
    occurred_at: str | None,
) -> list[CommandRecord]:
    found: list[CommandRecord] = []
    for part in _content_parts(payload):
        if part.get("type") != "tool_use":
            continue
        name = part.get("name")
        if not isinstance(name, str) or name not in _SHELL_TOOL_NAMES:
            continue
        inputs = part.get("input")
        if not isinstance(inputs, dict):
            continue
        command = inputs.get("command")
        if not isinstance(command, str):
            continue
        record = _parsed_command(
            occurred_at=occurred_at,
            tool=name,
            command=command,
            purpose=_purpose_from_mapping(inputs),
        )
        if record is not None:
            found.append(record)
    return found


def _codex_arguments(raw: object) -> dict[str, object]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _commands_from_codex_cmds(
    text: str,
    *,
    occurred_at: str | None,
    tool: str,
) -> list[CommandRecord]:
    bodies = [match.group(1) for match in _CODEX_CMD_DOUBLE.finditer(text)]
    bodies.extend(match.group(1) for match in _CODEX_CMD_SINGLE.finditer(text))
    found: list[CommandRecord] = []
    for body in bodies:
        record = _parsed_command(
            occurred_at=occurred_at,
            tool=tool,
            command=_unescape_cmd(body),
            purpose=None,
        )
        if record is not None:
            found.append(record)
    return found


def _commands_from_codex(
    payload: dict[str, object],
    *,
    occurred_at: str | None,
) -> list[CommandRecord]:
    if payload.get("type") != "response_item":
        return []
    nested = payload.get("payload")
    if not isinstance(nested, dict):
        return []
    nested_type = nested.get("type")
    name = nested.get("name")
    if nested_type == "function_call" and name in _CODEX_EXEC_NAMES:
        arguments = _codex_arguments(nested.get("arguments"))
        command = arguments.get("cmd")
        if not isinstance(command, str):
            command = arguments.get("command")
        if not isinstance(command, str):
            return []
        record = _parsed_command(
            occurred_at=occurred_at,
            tool=str(name),
            command=command,
            purpose=_purpose_from_mapping(arguments),
        )
        return [] if record is None else [record]
    if nested_type == "custom_tool_call" and name in _CODEX_EXEC_NAMES:
        raw_input = nested.get("input")
        if not isinstance(raw_input, str):
            return []
        return _commands_from_codex_cmds(
            raw_input,
            occurred_at=occurred_at,
            tool=str(name),
        )
    return []


def _extract_commands(
    agent: AgentName,
    payload: dict[str, object],
    *,
    occurred_at: str | None,
) -> list[CommandRecord]:
    if agent is AgentName.claude or agent is AgentName.cursor:
        return _commands_from_tool_use(payload, occurred_at=occurred_at)
    if agent is AgentName.codex:
        return _commands_from_codex(payload, occurred_at=occurred_at)
    return []


def _dedupe_commands(commands: list[CommandRecord]) -> list[CommandRecord]:
    seen: set[str] = set()
    unique: list[CommandRecord] = []
    for record in commands:
        if record.command in seen:
            continue
        seen.add(record.command)
        unique.append(record)
        if len(unique) >= MAX_COMMANDS_PER_SESSION:
            break
    return unique


def parse_transcript(agent: AgentName, path: Path) -> SessionRecord:
    texts: list[str] = []
    extracted: list[CommandRecord] = []
    project_cwd: str | None = None
    started_at: str | None = None
    last_timestamp: str | None = None
    body_full = False
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
                timestamp = payload.get("timestamp")
                if isinstance(timestamp, str) and timestamp.strip():
                    last_timestamp = timestamp
                    if started_at is None:
                        started_at = timestamp
                extracted_cwd = _extract_cwd(agent, payload)
                if extracted_cwd is not None and project_cwd is None:
                    project_cwd = extracted_cwd
                extracted.extend(
                    _extract_commands(
                        agent,
                        payload,
                        occurred_at=last_timestamp or started_at,
                    )
                )
                if body_full or not _is_indexable_event(agent, payload):
                    continue
                texts.extend(_walk_strings(payload))
                if sum(len(text) for text in texts) >= MAX_BODY_CHARS:
                    body_full = True
    except OSError:
        texts = []
        extracted = []

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
    started_at = _normalize_timestamp(started_at)
    source = str(path)
    commands = tuple(
        CommandRecord(
            agent=agent.value,
            source_path=source,
            occurred_at=item.occurred_at or started_at,
            tool=item.tool,
            command=item.command,
            purpose=item.purpose,
            kind=item.kind,
        )
        for item in _dedupe_commands(extracted)
    )
    return SessionRecord(
        agent=agent.value,
        source_path=source,
        mtime_ns=stat.st_mtime_ns,
        project_cwd=project_cwd,
        git_root=git_root,
        started_at=started_at,
        title=_first_user_title(texts),
        body=body,
        commands=commands,
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
    return None


def _role_for_event(agent: AgentName, payload: dict[str, object]) -> str | None:
    if agent is AgentName.claude:
        event_type = payload.get("type")
        if event_type in {"user", "assistant"}:
            return str(event_type)
        return None
    if agent is AgentName.cursor:
        role = payload.get("role")
        if role in {"user", "assistant"}:
            return str(role)
        return None
    if agent is AgentName.codex:
        event_type = payload.get("type")
        nested = payload.get("payload")
        if event_type == "event_msg" and isinstance(nested, dict):
            if nested.get("type") == "user_message":
                return "user"
            return None
        if event_type == "response_item" and isinstance(nested, dict):
            role = nested.get("role")
            if role in {"user", "assistant"}:
                return str(role)
        return None
    return None


def _text_from_content(content: object) -> list[str]:
    if isinstance(content, str):
        stripped = content.strip()
        return [stripped] if stripped else []
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for part in content:
        if isinstance(part, str):
            stripped = part.strip()
            if stripped:
                texts.append(stripped)
            continue
        if not isinstance(part, dict):
            continue
        if part.get("type") in {"tool_use", "tool_result"}:
            continue
        for key in ("text", "output_text", "message"):
            value = part.get(key)
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())
                break
    return texts


def _texts_from_payload(agent: AgentName, payload: dict[str, object]) -> list[str]:
    blobs: list[object] = []
    if agent is AgentName.codex:
        nested = payload.get("payload")
        if isinstance(nested, dict):
            message = nested.get("message")
            if isinstance(message, str):
                blobs.append(message)
            blobs.append(nested.get("content"))
    else:
        message = payload.get("message")
        if isinstance(message, dict):
            blobs.append(message.get("content"))
        elif isinstance(message, str):
            blobs.append(message)
        blobs.append(payload.get("content"))
    texts: list[str] = []
    for blob in blobs:
        texts.extend(_text_from_content(blob))
    return texts


def iter_transcript_turns(agent: AgentName, path: Path) -> list[Turn]:
    turns: list[Turn] = []
    last_timestamp: str | None = None
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
                timestamp = payload.get("timestamp")
                if isinstance(timestamp, str) and timestamp.strip():
                    last_timestamp = _normalize_timestamp(timestamp)
                occurred = last_timestamp
                role = _role_for_event(agent, payload)
                texts = _texts_from_payload(agent, payload) if role is not None else []
                if role is not None and texts:
                    turns.append(
                        Turn(role=role, text="\n".join(texts), occurred_at=occurred)
                    )
                for command in _extract_commands(agent, payload, occurred_at=occurred):
                    turns.append(
                        Turn(role="command", text=command.command, occurred_at=occurred)
                    )
    except OSError:
        return []
    return turns


def select_turns(
    turns: list[Turn],
    *,
    grep: str | None,
    context: int = 0,
) -> list[Turn]:
    if grep is None or not grep.strip():
        return turns
    needle = grep.strip().lower()
    matched = [index for index, turn in enumerate(turns) if needle in turn.text.lower()]
    if not matched:
        return []
    window = max(0, context)
    keep: set[int] = set()
    last = len(turns)
    for index in matched:
        start = max(0, index - window)
        end = min(last, index + window + 1)
        keep.update(range(start, end))
    return [turns[index] for index in sorted(keep)]


def clip_turn_text(text: str) -> str:
    if len(text) <= MAX_TURN_CHARS:
        return text
    return text[: MAX_TURN_CHARS - 3] + "..."


def format_turn(turn: Turn) -> str:
    return f"{turn.role}\n{clip_turn_text(turn.text)}"


def load_turns(path: Path) -> list[Turn]:
    if not path.is_file():
        raise FileNotFoundError(f"transcript not found: {path}")
    agent = agent_from_source_path(path)
    if agent is None:
        raise ValueError(f"cannot detect agent from path {path}")
    return iter_transcript_turns(agent, path)


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
    )


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


def reindex(
    layout: Layout,
    *,
    cwd: Path | None = None,
    all_projects: bool = False,
) -> tuple[int, int]:
    connection = connect(layout.history_db)
    stale_schema = _bump_schema_if_needed(connection)
    known: dict[str, int] = {} if stale_schema else existing_mtimes(connection)
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


def search(
    layout: Layout,
    query: str,
    *,
    cwd: Path | None = None,
    agent: str | None = None,
    all_projects: bool = False,
    limit: int = 20,
    sort: str = "relevance",
) -> list[SearchHit]:
    if sort not in SEARCH_SORTS:
        raise ValueError(f"unknown search sort {sort!r}")
    reindex(layout, cwd=cwd, all_projects=all_projects)
    connection = connect(layout.history_db)
    sql = f"""
        SELECT
            s.agent,
            s.source_path,
            s.project_cwd,
            s.git_root,
            s.started_at,
            s.title,
            s.body,
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
        sql += " ORDER BY s.started_at DESC LIMIT ?"
    else:
        sql += " ORDER BY rank ASC, s.started_at DESC LIMIT ?"
    params.append(_fetch_limit(cwd=cwd, all_projects=all_projects, limit=limit))
    rows = list(connection.execute(sql, params))
    connection.close()

    scope = None if all_projects else (cwd or Path.cwd())
    hits: list[SearchHit] = []
    for row in rows:
        record = _session_from_row(row)
        if scope is not None and not belongs_to_project(record, scope):
            continue
        snippet = _choose_snippet(row["body"], query, row["fts_snippet"] or "")
        hits.append(
            SearchHit(
                agent=row["agent"],
                source_path=row["source_path"],
                project_cwd=row["project_cwd"],
                started_at=row["started_at"],
                title=row["title"],
                snippet=snippet,
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
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[SearchHit]:
    reindex(layout, cwd=cwd, all_projects=all_projects)
    connection = connect(layout.history_db)
    sql = """
        SELECT agent, source_path, project_cwd, git_root, started_at, title, body
        FROM sessions
        WHERE 1 = 1
    """
    params: list[object] = []
    if agent is not None:
        sql += " AND agent = ?"
        params.append(agent)
    sql = _append_time_bounds(
        sql,
        params,
        column="started_at",
        since=since,
        until=until,
    )
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(_fetch_limit(cwd=cwd, all_projects=all_projects, limit=limit))
    rows = list(connection.execute(sql, params))
    connection.close()
    scope = None if all_projects else (cwd or Path.cwd())
    hits: list[SearchHit] = []
    for row in rows:
        record = _session_from_row(row)
        if scope is not None and not belongs_to_project(record, scope):
            continue
        if not _in_time_range(record.started_at, since=since, until=until):
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
) -> list[CommandRecord]:
    if kind is not None and kind not in COMMAND_KINDS:
        raise ValueError(f"unknown command kind {kind!r}")
    reindex(layout, cwd=cwd, all_projects=all_projects)
    connection = connect(layout.history_db)
    sql = """
        SELECT
            c.agent,
            c.source_path,
            c.occurred_at,
            c.tool,
            c.command,
            c.purpose,
            c.kind,
            s.project_cwd,
            s.git_root,
            s.started_at,
            s.title,
            s.body
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
    sql = _append_time_bounds(
        sql,
        params,
        column="c.occurred_at",
        since=since,
        until=until,
    )
    sql += " ORDER BY c.occurred_at DESC LIMIT ?"
    params.append(_fetch_limit(cwd=cwd, all_projects=all_projects, limit=limit))
    rows = list(connection.execute(sql, params))
    connection.close()
    scope = None if all_projects else (cwd or Path.cwd())
    hits: list[CommandRecord] = []
    for row in rows:
        record = _session_from_row(row)
        if scope is not None and not belongs_to_project(record, scope):
            continue
        occurred = row["occurred_at"] or row["started_at"]
        if not _in_time_range(occurred, since=since, until=until):
            continue
        hits.append(
            CommandRecord(
                agent=row["agent"],
                source_path=row["source_path"],
                occurred_at=occurred,
                tool=row["tool"],
                command=row["command"],
                purpose=row["purpose"],
                kind=row["kind"],
            )
        )
        if len(hits) >= limit:
            break
    return hits
