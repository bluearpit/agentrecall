from __future__ import annotations

import json
from pathlib import Path

from dotagents.history import list_sessions, reindex, search
from dotagents.layout import Layout


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_history_search_scoped_to_cwd(home: Path, layout: Layout, tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".git").mkdir()

    encoded = "".join(ch if ch.isalnum() else "-" for ch in str(project.resolve()))
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
