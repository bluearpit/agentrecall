from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from agentrecall.cli import app
from agentrecall.history import (
    classify_command_kind,
    encode_claude_project,
    encode_cursor_project,
    list_commands,
    list_sessions,
    reindex,
    search,
)
from agentrecall.layout import Layout

runner = CliRunner()


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".git").mkdir()
    return project


def test_history_search_scoped_to_cwd(home: Path, layout: Layout, tmp_path: Path) -> None:
    project = _project(tmp_path)

    encoded = encode_claude_project(str(project.resolve()))
    claude_file = home / ".claude" / "projects" / encoded / "sess.jsonl"
    _write_jsonl(
        claude_file,
        [
            {
                "type": "user",
                "cwd": str(project.resolve()),
                "timestamp": "2026-08-01T00:00:00Z",
                "message": {"role": "user", "content": "deploy the dagster pipeline"},
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            },
        ],
    )

    other = home / ".claude" / "projects" / "-other" / "sess.jsonl"
    _write_jsonl(
        other,
        [
            {
                "type": "user",
                "cwd": "/tmp/other",
                "timestamp": "2026-08-01T00:00:00Z",
                "message": {"role": "user", "content": "deploy the dagster pipeline"},
            }
        ],
    )

    indexed, _skipped = reindex(layout, cwd=project, all_projects=False)
    assert indexed >= 1
    hits = search(layout, "dagster pipeline", cwd=project, all_projects=False)
    assert hits
    assert all("/other" not in hit.source_path for hit in hits)
    listed = list_sessions(layout, cwd=project, all_projects=False)
    assert listed


def test_classify_command_kind() -> None:
    assert classify_command_kind("cd tests && pytest -k smoke") == "test"
    assert classify_command_kind("uv run pytest tests/test_history.py") == "test"
    assert classify_command_kind("curl -s https://example.com/health") == "http"
    assert classify_command_kind("git status --short") == "git"
    assert classify_command_kind("uv run python -c 'print(1)'") == "python"
    assert classify_command_kind("docker compose up -d") == "docker"
    assert classify_command_kind("ls -la") == "other"


def test_history_indexes_shell_commands(home: Path, layout: Layout, tmp_path: Path) -> None:
    project = _project(tmp_path)
    encoded = encode_claude_project(str(project.resolve()))
    _write_jsonl(
        home / ".claude" / "projects" / encoded / "sess.jsonl",
        [
            {
                "type": "user",
                "cwd": str(project.resolve()),
                "timestamp": "2026-08-10T00:00:00Z",
                "message": {"role": "user", "content": "run tests"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-10T00:01:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {
                                "command": "pytest -k smoke",
                                "description": "run smoke tests",
                            },
                        }
                    ],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-10T00:02:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {
                                "command": "curl -s http://127.0.0.1:8000/health",
                                "description": "hit local healthz",
                            },
                        }
                    ],
                },
            },
        ],
    )

    cursor_dir = (
        home
        / ".cursor"
        / "projects"
        / encode_cursor_project(str(project.resolve()))
        / "agent-transcripts"
        / "abc"
    )
    _write_jsonl(
        cursor_dir / "abc.jsonl",
        [
            {
                "role": "user",
                "message": {"content": [{"type": "text", "text": "status"}]},
            },
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Shell",
                            "input": {
                                "command": "git status --short",
                                "description": "check git status",
                            },
                        }
                    ]
                },
            },
        ],
    )

    commands = list_commands(layout, cwd=project, all_projects=False)
    kinds = {item.kind: item for item in commands}
    assert kinds["test"].command == "pytest -k smoke"
    assert kinds["test"].purpose == "run smoke tests"
    assert kinds["http"].command.startswith("curl")
    assert kinds["git"].agent == "cursor"

    tests_only = list_commands(layout, cwd=project, all_projects=False, kind="test")
    assert len(tests_only) == 1
    assert tests_only[0].kind == "test"


def test_history_indexes_codex_exec(home: Path, layout: Layout, tmp_path: Path) -> None:
    project = _project(tmp_path)
    session = (
        home
        / ".codex"
        / "sessions"
        / "2026"
        / "08"
        / "10"
        / "rollout.jsonl"
    )
    _write_jsonl(
        session,
        [
            {
                "timestamp": "2026-08-10T12:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "cwd": str(project.resolve()),
                    "message": "run it",
                },
            },
            {
                "timestamp": "2026-08-10T12:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps(
                        {
                            "cmd": "uv run pytest tests/test_history.py",
                            "justification": "run unit tests",
                        }
                    ),
                },
            },
            {
                "timestamp": "2026-08-10T12:02:00Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "input": (
                        'const r = await tools.exec_command({\n'
                        '  cmd: "curl -s https://example.com",\n'
                        '  workdir: "/tmp"\n'
                        "});\n"
                    ),
                },
            },
        ],
    )

    commands = list_commands(layout, cwd=project, all_projects=False)
    by_kind = {item.kind: item for item in commands}
    assert "test" in by_kind
    assert "http" in by_kind
    assert by_kind["test"].purpose == "run unit tests"


def test_history_list_since(home: Path, layout: Layout, tmp_path: Path) -> None:
    project = _project(tmp_path)
    encoded = encode_claude_project(str(project.resolve()))
    _write_jsonl(
        home / ".claude" / "projects" / encoded / "old.jsonl",
        [
            {
                "type": "user",
                "cwd": str(project.resolve()),
                "timestamp": "2026-07-01T00:00:00Z",
                "message": {"role": "user", "content": "old work"},
            }
        ],
    )
    _write_jsonl(
        home / ".claude" / "projects" / encoded / "new.jsonl",
        [
            {
                "type": "user",
                "cwd": str(project.resolve()),
                "timestamp": "2026-08-10T00:00:00Z",
                "message": {"role": "user", "content": "new work"},
            }
        ],
    )

    since = datetime(2026, 8, 1, tzinfo=UTC)
    listed = list_sessions(layout, cwd=project, all_projects=False, since=since)
    assert len(listed) == 1
    assert listed[0].started_at == "2026-08-10T00:00:00Z"
    assert "new work" in listed[0].snippet

    until = datetime(2026, 7, 31, 23, 59, 59, tzinfo=UTC)
    older = list_sessions(layout, cwd=project, all_projects=False, until=until)
    assert len(older) == 1
    assert older[0].started_at == "2026-07-01T00:00:00Z"


def test_history_commands_cli(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path)
    encoded = encode_claude_project(str(project.resolve()))
    _write_jsonl(
        home / ".claude" / "projects" / encoded / "sess.jsonl",
        [
            {
                "type": "user",
                "cwd": str(project.resolve()),
                "timestamp": "2026-08-10T00:00:00Z",
                "message": {"role": "user", "content": "test"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-10T00:01:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "pytest", "description": "tests"},
                        }
                    ],
                },
            },
        ],
    )

    listed = runner.invoke(
        app,
        ["history", "list", "--cwd", str(project), "--since", "2026-08-01"],
    )
    assert listed.exit_code == 0
    assert "2026-08-10T00:00:00Z" in listed.stdout

    too_new = runner.invoke(
        app,
        ["history", "list", "--cwd", str(project), "--since", "2026-08-15"],
    )
    assert too_new.exit_code == 0
    assert "no sessions" in too_new.stdout

    commands = runner.invoke(
        app,
        ["history", "commands", "--cwd", str(project), "--kind", "test"],
    )
    assert commands.exit_code == 0
    assert "pytest" in commands.stdout
    assert "tests" in commands.stdout

    bad = runner.invoke(
        app,
        ["history", "list", "--cwd", str(project), "--since", "not-a-date"],
    )
    assert bad.exit_code == 1
