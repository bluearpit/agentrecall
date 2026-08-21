from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from agentrecall.cli import app
from agentrecall.history import (
    _snippet,
    classify_command_kind,
    connect,
    decode_encoded_project,
    encode_claude_project,
    encode_cursor_project,
    infer_cwd_from_source_path,
    list_commands,
    list_sessions,
    load_turns,
    parse_transcript,
    reindex,
    search,
    select_turns,
)
from agentrecall.layout import AgentName, Layout

runner = CliRunner()


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".git").mkdir()
    return project


def _hyphen_tree(root: Path) -> tuple[Path, Path]:
    work = root / "Users" / "alice" / "work"
    my_app = work / "my-app"
    my_app_web = work / "my-app-web"
    my_app.mkdir(parents=True)
    my_app_web.mkdir(parents=True)
    (my_app / ".git").mkdir()
    (my_app_web / ".git").mkdir()
    return my_app, my_app_web


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


def test_encode_cursor_project_keeps_underscores() -> None:
    assert encode_cursor_project("/Users/alice/my_app") == "Users-alice-my_app"


def test_decode_encoded_project_keeps_hyphenated_leaf(tmp_path: Path) -> None:
    my_app, my_app_web = _hyphen_tree(tmp_path)
    decoded_app = decode_encoded_project("Users-alice-work-my-app", root=tmp_path)
    decoded_web = decode_encoded_project("Users-alice-work-my-app-web", root=tmp_path)
    assert decoded_app == my_app
    assert decoded_web == my_app_web
    assert decode_encoded_project("Users-alice-work-missing-app", root=tmp_path) is None


def test_infer_cursor_cwd_from_hyphenated_dir(tmp_path: Path) -> None:
    my_app, _web = _hyphen_tree(tmp_path)
    source = (
        tmp_path
        / "home"
        / ".cursor"
        / "projects"
        / "Users-alice-work-my-app"
        / "agent-transcripts"
        / "abc"
        / "abc.jsonl"
    )
    source.parent.mkdir(parents=True)
    source.write_text("{}\n", encoding="utf-8")
    inferred = infer_cwd_from_source_path(AgentName.cursor, source, root=tmp_path)
    assert inferred == str(my_app)
    assert inferred is not None
    assert not inferred.endswith("/my/app")


def test_parse_transcript_preserves_hyphenated_cursor_cwd(
    home: Path, tmp_path: Path
) -> None:
    project = tmp_path / "my-app"
    project.mkdir()
    (project / ".git").mkdir()
    source = (
        home
        / ".cursor"
        / "projects"
        / encode_cursor_project(str(project.resolve()))
        / "agent-transcripts"
        / "abc"
        / "abc.jsonl"
    )
    _write_jsonl(
        source,
        [
            {
                "role": "user",
                "message": {"content": [{"type": "text", "text": "ship the adapter"}]},
            }
        ],
    )
    record = parse_transcript(AgentName.cursor, source)
    assert record.project_cwd is not None
    assert Path(record.project_cwd).resolve() == project.resolve()
    assert Path(record.project_cwd).name == "my-app"


def test_parse_transcript_infers_hyphenated_claude_cwd(
    home: Path, tmp_path: Path
) -> None:
    project = tmp_path / "my-app"
    project.mkdir()
    (project / ".git").mkdir()
    source = (
        home / ".claude" / "projects" / encode_claude_project(str(project.resolve())) / "sess.jsonl"
    )
    _write_jsonl(
        source,
        [
            {
                "type": "user",
                "timestamp": "2026-08-01T00:00:00Z",
                "message": {"role": "user", "content": "ship the adapter"},
            }
        ],
    )
    record = parse_transcript(AgentName.claude, source)
    assert record.project_cwd is not None
    assert Path(record.project_cwd).resolve() == project.resolve()


def test_cwd_schema_bump_reparses_unchanged_mtime(
    home: Path, layout: Layout, tmp_path: Path
) -> None:
    project = tmp_path / "my-app"
    project.mkdir()
    (project / ".git").mkdir()
    source = (
        home
        / ".cursor"
        / "projects"
        / encode_cursor_project(str(project.resolve()))
        / "agent-transcripts"
        / "abc"
        / "abc.jsonl"
    )
    _write_jsonl(
        source,
        [{"role": "user", "message": {"content": [{"type": "text", "text": "hello"}]}}],
    )
    reindex(layout, cwd=project, all_projects=False)
    connection = connect(layout.history_db)
    connection.execute("PRAGMA user_version = 2")
    connection.execute(
        "UPDATE sessions SET project_cwd = ? WHERE source_path = ?",
        ("/wrong/my/app", str(source)),
    )
    connection.commit()
    connection.close()

    reindex(layout, cwd=project, all_projects=False)
    connection = connect(layout.history_db)
    row = connection.execute(
        "SELECT project_cwd FROM sessions WHERE source_path = ?",
        (str(source),),
    ).fetchone()
    connection.close()
    assert row is not None
    assert Path(row["project_cwd"]).resolve() == project.resolve()


def test_hyphenated_sibling_is_not_pulled_into_cwd_scope(
    home: Path, layout: Layout, tmp_path: Path
) -> None:
    my_app = tmp_path / "my-app"
    my_app_web = tmp_path / "my-app-web"
    my_app.mkdir()
    my_app_web.mkdir()
    (my_app / ".git").mkdir()
    (my_app_web / ".git").mkdir()
    _write_jsonl(
        home
        / ".cursor"
        / "projects"
        / encode_cursor_project(str(my_app.resolve()))
        / "agent-transcripts"
        / "aaa"
        / "aaa.jsonl",
        [{"role": "user", "message": {"content": [{"type": "text", "text": "adapter in app"}]}}],
    )
    _write_jsonl(
        home
        / ".cursor"
        / "projects"
        / encode_cursor_project(str(my_app_web.resolve()))
        / "agent-transcripts"
        / "bbb"
        / "bbb.jsonl",
        [{"role": "user", "message": {"content": [{"type": "text", "text": "adapter in web"}]}}],
    )
    hits = search(layout, "adapter", cwd=my_app, all_projects=False)
    assert hits
    assert all("my-app-web" not in hit.source_path for hit in hits)
    assert all(hit.project_cwd is not None for hit in hits)
    assert all(Path(hit.project_cwd).name == "my-app" for hit in hits)


def test_show_normalizes_cursor_claude_and_codex(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path)
    cursor = (
        home
        / ".cursor"
        / "projects"
        / encode_cursor_project(str(project.resolve()))
        / "agent-transcripts"
        / "abc"
        / "abc.jsonl"
    )
    _write_jsonl(
        cursor,
        [
            {
                "role": "user",
                "message": {"content": [{"type": "text", "text": "ship the adapter"}]},
            },
            {
                "role": "assistant",
                "message": {"content": [{"type": "text", "text": "working on it"}]},
            },
        ],
    )
    claude = (
        home / ".claude" / "projects" / encode_claude_project(str(project.resolve())) / "s.jsonl"
    )
    _write_jsonl(
        claude,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "ship the adapter"},
            }
        ],
    )
    codex = home / ".codex" / "sessions" / "2026" / "08" / "01" / "rollout.jsonl"
    _write_jsonl(
        codex,
        [
            {
                "timestamp": "2026-08-01T12:00:00Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "ship the adapter"},
            },
            {
                "timestamp": "2026-08-01T12:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "on it"}],
                },
            },
        ],
    )
    assert load_turns(cursor)[0].text == "ship the adapter"
    assert load_turns(claude)[0].role == "user"
    assert load_turns(claude)[0].text == "ship the adapter"
    codex_turns = load_turns(codex)
    assert [turn.role for turn in codex_turns] == ["user", "assistant"]
    assert codex_turns[1].text == "on it"


def test_show_grep_and_context(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path)
    source = (
        home / ".claude" / "projects" / encode_claude_project(str(project.resolve())) / "sess.jsonl"
    )
    _write_jsonl(
        source,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "status update"},
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "all clear"}],
                },
            },
            {
                "type": "user",
                "message": {"role": "user", "content": "ship the adapter"},
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
                            "input": {"command": "pytest -k adapter", "description": "tests"},
                        }
                    ],
                },
            },
        ],
    )
    turns = load_turns(source)
    matched = select_turns(turns, grep="adapter", context=0)
    assert [turn.role for turn in matched] == ["user", "command"]
    assert matched[0].text == "ship the adapter"
    assert matched[1].text == "pytest -k adapter"
    with_context = select_turns(turns, grep="adapter", context=1)
    assert any(turn.text == "all clear" for turn in with_context)
    assert select_turns(turns, grep="missing-term") == []


def test_history_show_cli(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path)
    source = (
        home / ".claude" / "projects" / encode_claude_project(str(project.resolve())) / "sess.jsonl"
    )
    _write_jsonl(
        source,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "status update"},
            },
            {
                "type": "user",
                "message": {"role": "user", "content": "ship the adapter"},
            },
        ],
    )
    shown = runner.invoke(app, ["history", "show", str(source), "--grep", "adapter"])
    assert shown.exit_code == 0
    assert "ship the adapter" in shown.stdout
    assert "status update" not in shown.stdout

    missing = runner.invoke(app, ["history", "show", str(source), "--grep", "nope"])
    assert missing.exit_code == 0
    assert "no matches" in missing.stdout

    gone = runner.invoke(app, ["history", "show", str(tmp_path / "missing.jsonl")])
    assert gone.exit_code == 1


def test_show_does_not_copy_transcript_into_index(
    home: Path, layout: Layout, tmp_path: Path
) -> None:
    project = _project(tmp_path)
    source = (
        home / ".claude" / "projects" / encode_claude_project(str(project.resolve())) / "sess.jsonl"
    )
    _write_jsonl(
        source,
        [{"type": "user", "message": {"role": "user", "content": "ship the adapter"}}],
    )
    reindex(layout, cwd=project, all_projects=False)
    connection = connect(layout.history_db)
    before = list(connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
    count = connection.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
    connection.close()
    shown = runner.invoke(app, ["history", "show", str(source)])
    assert shown.exit_code == 0
    connection = connect(layout.history_db)
    after = list(connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
    after_count = connection.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
    connection.close()
    assert after == before
    assert after_count == count


def test_snippet_prefers_later_query_term() -> None:
    body = "widget " + ("filler " * 80) + " see runbook for the steps"
    snippet = _snippet(body, "widget runbook")
    assert "runbook" in snippet


def test_search_ranks_focused_session_ahead_of_recent_filler(
    home: Path, layout: Layout, tmp_path: Path
) -> None:
    project = _project(tmp_path)
    encoded = encode_claude_project(str(project.resolve()))
    _write_jsonl(
        home / ".claude" / "projects" / encoded / "focused.jsonl",
        [
            {
                "type": "user",
                "cwd": str(project.resolve()),
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "widget release notes"},
            }
        ],
    )
    _write_jsonl(
        home / ".claude" / "projects" / encoded / "recent.jsonl",
        [
            {
                "type": "user",
                "cwd": str(project.resolve()),
                "timestamp": "2026-08-20T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": ("the " * 400) + "widget release buried in filler",
                },
            }
        ],
    )
    relevant = search(layout, "widget release", cwd=project, all_projects=False)
    assert relevant
    assert relevant[0].source_path.endswith("focused.jsonl")

    recent = search(
        layout,
        "widget release",
        cwd=project,
        all_projects=False,
        sort="recent",
    )
    assert recent[0].source_path.endswith("recent.jsonl")


def test_search_snippet_includes_later_term(
    home: Path, layout: Layout, tmp_path: Path
) -> None:
    project = _project(tmp_path)
    encoded = encode_claude_project(str(project.resolve()))
    _write_jsonl(
        home / ".claude" / "projects" / encoded / "sess.jsonl",
        [
            {
                "type": "user",
                "cwd": str(project.resolve()),
                "timestamp": "2026-08-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": "widget " + ("filler " * 80) + " see runbook for the steps",
                },
            }
        ],
    )
    hits = search(layout, "widget runbook", cwd=project, all_projects=False)
    assert hits
    assert "runbook" in hits[0].snippet


def test_search_rejects_unknown_sort(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = runner.invoke(
        app,
        ["history", "search", "widget", "--cwd", str(project), "--sort", "nope"],
    )
    assert result.exit_code == 1
