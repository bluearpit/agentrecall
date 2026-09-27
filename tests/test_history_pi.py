from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentrecall.cli import app
from agentrecall.history import list_commands, list_sessions, reindex, search
from agentrecall.layout import Layout


def _write_pi_session(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def test_pi_sessions_join_catalog_and_json_api(home: Path, layout: Layout, tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / ".git").mkdir()
    (second / ".git").mkdir()
    named = home / ".pi" / "agent" / "sessions" / "--first--" / "named.jsonl"
    unnamed = home / ".pi" / "agent" / "sessions" / "--second--" / "unnamed.jsonl"
    _write_pi_session(
        named,
        [
            {
                "type": "session",
                "version": 3,
                "id": "pi-named-id",
                "timestamp": "2026-09-26T10:00:00Z",
                "cwd": str(first),
            },
            {
                "type": "message",
                "timestamp": "2026-09-26T10:01:00Z",
                "message": {"role": "user", "content": "Investigate authentication cache"},
            },
            {"type": "session_info", "timestamp": "2026-09-26T10:02:00Z", "name": "Auth cache"},
            {"type": "session_info", "timestamp": "2026-09-26T10:03:00Z", "name": "Auth cache fix"},
            {
                "type": "message",
                "timestamp": "2026-09-26T10:04:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Cache fixed"},
                        {
                            "type": "toolCall",
                            "name": "bash",
                            "arguments": {"command": "pytest tests/test_auth.py"},
                        },
                    ],
                },
            },
        ],
    )
    _write_pi_session(
        unnamed,
        [
            {
                "type": "session",
                "version": 3,
                "id": "pi-unnamed-id",
                "timestamp": "2026-09-27T10:00:00Z",
                "cwd": str(second),
            },
            {
                "type": "message",
                "timestamp": "2026-09-27T10:01:00Z",
                "message": {"role": "user", "content": "Inspect payment retries"},
            },
        ],
    )

    indexed, _skipped = reindex(layout, all_projects=True)
    assert indexed == 2
    assert {hit.source_path for hit in list_sessions(layout, all_projects=True, agent="pi")} == {
        str(named),
        str(unnamed),
    }
    assert [hit.source_path for hit in list_sessions(layout, cwd=first, agent="pi")] == [str(named)]
    assert [hit.source_path for hit in search(layout, "authentication", cwd=first, agent="pi")] == [
        str(named)
    ]
    assert [item.command for item in list_commands(layout, cwd=first, agent="pi")] == [
        "pytest tests/test_auth.py"
    ]

    runner = CliRunner()
    listed = runner.invoke(app, ["history", "list", "--all", "--agent", "pi", "--format", "json"])
    assert listed.exit_code == 0, listed.output
    catalog = json.loads(listed.stdout)
    assert catalog["schema_version"] == 1
    sessions = {entry["source_path"]: entry for entry in catalog["sessions"]}
    assert sessions[str(named)] == {
        "agent": "pi",
        "session_id": "pi-named-id",
        "source_path": str(named),
        "project_cwd": str(first),
        "name": "Auth cache fix",
        "title": "Auth cache fix",
        "started_at": "2026-09-26T10:00:00Z",
        "updated_at": "2026-09-26T10:04:00Z",
        "capabilities": {"searchable": True, "resumable": True},
    }
    assert sessions[str(unnamed)]["name"] is None
    assert sessions[str(unnamed)]["title"] == "Inspect payment retries"
    assert sessions[str(unnamed)]["project_cwd"] == str(second)

    searched = runner.invoke(
        app, ["history", "search", "authentication", "--all", "--agent", "pi", "--format", "json"]
    )
    assert searched.exit_code == 0, searched.output
    match = json.loads(searched.stdout)["matches"][0]
    assert match["session_id"] == "pi-named-id"
    assert "authentication" in match["snippet"].lower()

    shown = runner.invoke(app, ["history", "show", str(named), "--grep", "authentication"])
    assert shown.exit_code == 0, shown.output
    assert "Investigate authentication cache" in shown.stdout
    assert "Cache fixed" not in shown.stdout
    command_turn = runner.invoke(app, ["history", "show", str(named), "--grep", "pytest"])
    assert "pytest tests/test_auth.py" in command_turn.stdout


def test_pi_malformed_entries_and_custom_session_directory(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    sessions_dir = home / "custom-pi-sessions"
    source = sessions_dir / "partial.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session",
                        "id": "partial-id",
                        "timestamp": "2026-09-27T12:00:00Z",
                        "cwd": str(project),
                    }
                ),
                "{broken json",
                json.dumps({"type": "message", "message": {"role": "assistant", "content": []}}),
                json.dumps(
                    {
                        "type": "message",
                        "timestamp": "2026-09-27T12:01:00Z",
                        "message": {"role": "user", "content": "Recover partial session"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    incomplete = sessions_dir / "incomplete.jsonl"
    _write_pi_session(
        incomplete,
        [
            {"type": "session", "cwd": str(project), "timestamp": "2026-09-27T11:00:00Z"},
            {
                "type": "message",
                "message": {"role": "user", "content": "Recover unnamed metadata"},
            },
        ],
    )
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(sessions_dir))
    layout = Layout.from_environ()
    indexed, _skipped = reindex(layout, all_projects=True)
    assert indexed == 2
    hits = {hit.source_path: hit for hit in list_sessions(layout, cwd=project, agent="pi")}
    hit = hits[str(source)]
    assert hit.title == "Recover partial session"
    assert hit.session_id == "partial-id"
    assert hit.updated_at == "2026-09-27T12:01:00Z"
    assert hits[str(incomplete)].title == "Recover unnamed metadata"
    assert hits[str(incomplete)].resumable is False
    shown = CliRunner().invoke(app, ["history", "show", str(source)])
    assert shown.exit_code == 0, shown.output
    assert "Recover partial session" in shown.stdout


def test_deleted_pi_session_is_removed_from_catalog_search_and_commands(
    layout: Layout, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = layout.pi_sessions_dir / "deleted.jsonl"
    _write_pi_session(
        source,
        [
            {"type": "session", "id": "deleted-id", "cwd": str(project)},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "orphaned session"},
                        {
                            "type": "toolCall",
                            "name": "bash",
                            "arguments": {"command": "git status"},
                        },
                    ],
                },
            },
        ],
    )
    assert list_sessions(layout, all_projects=True, agent="pi")[0].resumable is True
    assert search(layout, "orphaned", all_projects=True, agent="pi")
    assert list_commands(layout, all_projects=True, agent="pi")

    source.unlink()

    assert list_sessions(layout, all_projects=True, agent="pi") == []
    assert search(layout, "orphaned", all_projects=True, agent="pi") == []
    assert list_commands(layout, all_projects=True, agent="pi") == []
    with sqlite3.connect(layout.history_db) as connection:
        for table in ("sessions", "sessions_fts", "commands"):
            count = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE source_path = ?", (str(source),)
            ).fetchone()[0]
            assert count == 0


def test_recent_pi_sessions_use_last_activity_before_limit(layout: Layout, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    active = layout.pi_sessions_dir / "active.jsonl"
    idle = layout.pi_sessions_dir / "idle.jsonl"
    _write_pi_session(
        active,
        [
            {
                "type": "session",
                "id": "active-id",
                "cwd": str(project),
                "timestamp": "2026-09-01T00:00:00Z",
            },
            {
                "type": "message",
                "timestamp": "2026-09-27T00:00:00Z",
                "message": {"role": "user", "content": "session activity"},
            },
        ],
    )
    _write_pi_session(
        idle,
        [
            {
                "type": "session",
                "id": "idle-id",
                "cwd": str(project),
                "timestamp": "2026-09-20T00:00:00Z",
            },
            {"type": "message", "message": {"role": "user", "content": "session activity"}},
        ],
    )
    reindex(layout, all_projects=True)
    with sqlite3.connect(layout.history_db) as connection:
        connection.execute(
            "UPDATE sessions SET updated_at = NULL WHERE source_path = ?", (str(idle),)
        )

    assert [
        hit.source_path for hit in list_sessions(layout, all_projects=True, agent="pi", limit=1)
    ] == [str(active)]
    since = datetime(2026, 9, 25, tzinfo=UTC)
    assert [
        hit.source_path for hit in list_sessions(layout, all_projects=True, agent="pi", since=since)
    ] == [str(active)]
    assert [
        hit.source_path
        for hit in search(layout, "activity", all_projects=True, agent="pi", sort="recent", limit=1)
    ] == [str(active)]


def test_pi_session_without_cwd_is_not_advertised_as_resumable(
    layout: Layout,
) -> None:
    source = layout.pi_sessions_dir / "missing-cwd.jsonl"
    _write_pi_session(
        source,
        [
            {"type": "session", "id": "has-id", "timestamp": "2026-09-27T00:00:00Z"},
            {"type": "message", "message": {"role": "user", "content": "missing project cwd"}},
        ],
    )
    hit = list_sessions(layout, all_projects=True, agent="pi")[0]
    assert hit.session_id == "has-id"
    assert hit.project_cwd is None
    assert hit.resumable is False

    # An index written by the previous version may still have the old flag.
    with sqlite3.connect(layout.history_db) as connection:
        connection.execute(
            "UPDATE sessions SET resumable = 1 WHERE source_path = ?", (str(source),)
        )
    listed = CliRunner().invoke(
        app, ["history", "list", "--all", "--agent", "pi", "--format", "json"]
    )
    assert listed.exit_code == 0, listed.output
    entry = json.loads(listed.stdout)["sessions"][0]
    assert entry["project_cwd"] is None
    assert entry["capabilities"]["resumable"] is False


def test_pi_agent_directory_override(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_dir = home / "alternate-pi"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    layout = Layout.from_environ()
    assert layout.pi_agent_dir == agent_dir
    assert layout.pi_sessions_dir == agent_dir / "sessions"


def test_legacy_index_adds_catalog_columns(layout: Layout, tmp_path: Path) -> None:
    project = tmp_path / "legacy-project"
    project.mkdir()
    source = layout.pi_sessions_dir / "legacy.jsonl"
    _write_pi_session(
        source,
        [
            {"type": "session", "id": "legacy-pi", "cwd": str(project)},
            {"type": "message", "message": {"role": "user", "content": "Legacy session"}},
        ],
    )
    layout.history_db.parent.mkdir(parents=True)
    with sqlite3.connect(layout.history_db) as connection:
        connection.execute(
            """
            CREATE TABLE sessions (
                source_path TEXT PRIMARY KEY, agent TEXT NOT NULL, mtime_ns INTEGER NOT NULL,
                project_cwd TEXT, git_root TEXT, started_at TEXT, title TEXT NOT NULL,
                body TEXT NOT NULL
            )
            """
        )
        connection.execute("PRAGMA user_version = 3")

    indexed, _skipped = reindex(layout, all_projects=True)
    assert indexed == 1
    hit = list_sessions(layout, all_projects=True, agent="pi")[0]
    assert hit.session_id == "legacy-pi"
    assert hit.resumable is True
