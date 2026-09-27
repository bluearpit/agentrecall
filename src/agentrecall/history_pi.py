"""Read Pi's public JSONL session format without depending on Pi internals."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path


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
