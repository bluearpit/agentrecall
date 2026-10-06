"""Parse native transcript formats into shared session and turn records."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from agentrecall.history_models import CommandRecord, SessionRecord, Turn
from agentrecall.history_sources import (
    agent_from_source_path,
    describe_checkout,
    infer_cwd_from_source_path,
)
from agentrecall.layout import AgentName, Layout

MAX_BODY_CHARS = 200_000
MAX_COMMAND_CHARS = 8_000
MAX_COMMANDS_PER_SESSION = 1_000
MAX_TURN_CHARS = 8_000
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
    if agent is AgentName.pi:
        return _parse_pi_transcript(path)
    texts: list[str] = []
    extracted: list[CommandRecord] = []
    project_cwd: str | None = None
    branch: str | None = None
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
                extracted_branch = _extract_branch(agent, payload)
                if extracted_branch is not None:
                    branch = extracted_branch
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

    git_root, repo_root, head_branch = describe_checkout(project_cwd)
    if branch is None:
        branch = head_branch

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
        updated_at=_normalize_timestamp(last_timestamp) or started_at,
        repo_root=repo_root,
        branch=branch,
    )


def iter_pi_events(path: Path) -> Iterator[dict[str, object]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def pi_message_text(event: dict[str, object]) -> tuple[str, str] | None:
    if event.get("type") != "message":
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role not in {"user", "assistant"}:
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        text = "\n".join(
            part["text"].strip()
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
            and part["text"].strip()
        )
    else:
        return None
    return (role, text) if text else None


def pi_shell_commands(event: dict[str, object]) -> Iterator[tuple[str, str]]:
    if event.get("type") != "message":
        return
    message = event.get("message")
    if not isinstance(message, dict):
        return
    if message.get("role") == "bashExecution":
        command = message.get("command")
        if isinstance(command, str) and command.strip():
            yield "bash", command
        return
    if message.get("role") != "assistant":
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "toolCall":
            continue
        name = part.get("name")
        if name not in {"bash", "powershell"}:
            continue
        arguments = part.get("arguments")
        if not isinstance(arguments, dict):
            continue
        command = arguments.get("command")
        if isinstance(command, str) and command.strip():
            yield str(name), command


def _parse_pi_transcript(path: Path) -> SessionRecord:
    session_id: str | None = None
    project_cwd: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    name: str | None = None
    first_user_text: str | None = None
    texts: list[str] = []
    extracted: list[CommandRecord] = []
    text_length = 0
    try:
        for event in iter_pi_events(path):
            event_type = event.get("type")
            timestamp = event.get("timestamp")
            if isinstance(timestamp, str) and timestamp.strip():
                updated_at = _normalize_timestamp(timestamp)
            if event_type == "session" and started_at is None:
                raw_id = event.get("id")
                if isinstance(raw_id, str) and raw_id.strip():
                    session_id = raw_id.strip()
                raw_cwd = event.get("cwd")
                if isinstance(raw_cwd, str) and Path(raw_cwd).is_absolute():
                    project_cwd = raw_cwd
                started_at = updated_at
            elif event_type == "session_info":
                raw_name = event.get("name")
                name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
            for tool, command in pi_shell_commands(event):
                record = _parsed_command(
                    occurred_at=updated_at,
                    tool=tool,
                    command=command,
                    purpose=None,
                )
                if record is not None:
                    extracted.append(record)
            message = pi_message_text(event)
            if message is None:
                continue
            role, message_text = message
            if role == "user" and first_user_text is None:
                first_user_text = message_text
            if text_length < MAX_BODY_CHARS:
                clipped = message_text[: MAX_BODY_CHARS - text_length]
                texts.append(clipped)
                text_length += len(clipped)
    except OSError:
        pass

    stat = path.stat()
    fallback_time = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
    started_at = started_at or _normalize_timestamp(fallback_time)
    updated_at = updated_at or _normalize_timestamp(fallback_time)
    git_root, repo_root, branch = describe_checkout(project_cwd)
    return SessionRecord(
        agent=AgentName.pi.value,
        source_path=str(path),
        mtime_ns=stat.st_mtime_ns,
        project_cwd=project_cwd,
        git_root=git_root,
        started_at=started_at,
        title=name or _first_user_title([first_user_text] if first_user_text else []),
        body="\n".join(texts)[:MAX_BODY_CHARS],
        commands=tuple(
            CommandRecord(
                agent=AgentName.pi.value,
                source_path=str(path),
                occurred_at=item.occurred_at or started_at,
                tool=item.tool,
                command=item.command,
                purpose=item.purpose,
                kind=item.kind,
            )
            for item in _dedupe_commands(extracted)
        ),
        session_id=session_id,
        name=name,
        updated_at=updated_at,
        resumable=session_id is not None and project_cwd is not None,
        repo_root=repo_root,
        branch=branch,
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


def _extract_branch(agent: AgentName, payload: dict[str, object]) -> str | None:
    """Branch metadata the agent wrote itself: Claude ``gitBranch`` or Codex ``git.branch``.

    Claude stamps every event, so the caller keeps the last value seen and a
    session that switched branches is filed under the branch it ended on.
    """
    if agent is AgentName.claude:
        branch = payload.get("gitBranch")
        return branch if isinstance(branch, str) and branch.strip() else None
    if agent is AgentName.codex:
        nested = payload.get("payload")
        if isinstance(nested, dict):
            git_meta = nested.get("git")
            if isinstance(git_meta, dict):
                branch = git_meta.get("branch")
                return branch if isinstance(branch, str) and branch.strip() else None
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
    if agent is AgentName.pi:
        return _pi_transcript_turns(path)
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
                    turns.append(Turn(role=role, text="\n".join(texts), occurred_at=occurred))
                for command in _extract_commands(agent, payload, occurred_at=occurred):
                    turns.append(Turn(role="command", text=command.command, occurred_at=occurred))
    except OSError:
        return []
    return turns


def _pi_transcript_turns(path: Path) -> list[Turn]:
    turns: list[Turn] = []
    try:
        for event in iter_pi_events(path):
            message = pi_message_text(event)
            timestamp = event.get("timestamp")
            occurred_at = _normalize_timestamp(timestamp) if isinstance(timestamp, str) else None
            if message is not None:
                turns.append(Turn(role=message[0], text=message[1], occurred_at=occurred_at))
            for _tool, command in pi_shell_commands(event):
                turns.append(Turn(role="command", text=command, occurred_at=occurred_at))
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
    if agent is None and path.resolve().is_relative_to(
        Layout.from_environ().pi_sessions_dir.resolve()
    ):
        agent = AgentName.pi
    if agent is None:
        raise ValueError(f"cannot detect agent from path {path}")
    return iter_transcript_turns(agent, path)
