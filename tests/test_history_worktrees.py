from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentrecall.cli import app
from agentrecall.history import (
    connect,
    encode_claude_project,
    encode_cursor_project,
    git_head_branch,
    git_repo_root,
    list_commands,
    list_sessions,
    list_worktrees,
    parse_transcript,
    project_match_keys,
    project_scope,
    registered_worktrees,
    search,
)
from agentrecall.history_models import SessionRecord
from agentrecall.layout import AgentName, Layout

runner = CliRunner()


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _repo(tmp_path: Path, name: str = "app", *, branch: str = "main") -> Path:
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    return repo


def _worktree(repo: Path, name: str, *, branch: str | None) -> Path:
    """Lay out a linked worktree the way `git worktree add` does, without running git."""
    checkout = repo.parent / f"{repo.name}-{name}"
    checkout.mkdir()
    admin = repo / ".git" / "worktrees" / name
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{checkout / '.git'}\n", encoding="utf-8")
    head = f"ref: refs/heads/{branch}\n" if branch else "0123456789abcdef0123456789abcdef01234567\n"
    (admin / "HEAD").write_text(head, encoding="utf-8")
    (checkout / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    return checkout


def _claude_session(home: Path, cwd: Path, name: str, text: str, *, branch: str) -> Path:
    encoded = encode_claude_project(str(cwd.resolve()))
    path = home / ".claude" / "projects" / encoded / f"{name}.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "cwd": str(cwd.resolve()),
                "gitBranch": branch,
                "timestamp": "2026-09-01T10:00:00Z",
                "message": {"role": "user", "content": text},
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": f"pytest tests/test_{name}.py"},
                        }
                    ],
                },
            },
        ],
    )
    return path


def _cursor_session(home: Path, cwd: Path, name: str, text: str) -> Path:
    path = (
        home
        / ".cursor"
        / "projects"
        / encode_cursor_project(str(cwd.resolve()))
        / "agent-transcripts"
        / name
        / f"{name}.jsonl"
    )
    _write_jsonl(
        path,
        [{"role": "user", "message": {"content": [{"type": "text", "text": text}]}}],
    )
    return path


def test_git_repo_root_follows_worktree_pointer(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    linked = _worktree(repo, "feature", branch="feature/one")
    detached = _worktree(repo, "review", branch=None)

    assert git_repo_root(repo) == repo
    assert git_repo_root(linked) == repo.resolve()
    assert git_repo_root(detached) == repo.resolve()
    assert git_head_branch(repo) == "main"
    assert git_head_branch(linked) == "feature/one"
    assert git_head_branch(detached) is None
    assert registered_worktrees(repo) == [linked.resolve(), detached.resolve()]


def test_git_repo_root_without_commondir_uses_worktrees_layout(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    linked = _worktree(repo, "feature", branch="feature/one")
    (repo / ".git" / "worktrees" / "feature" / "commondir").unlink()

    assert git_repo_root(linked) == repo.resolve()


def test_git_repo_root_is_none_for_dangling_pointer(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    orphan = tmp_path / "app-gone"
    orphan.mkdir()
    (orphan / ".git").write_text(f"gitdir: {repo / '.git' / 'worktrees' / 'gone'}\n")

    assert git_repo_root(orphan) is None


def test_match_keys_span_the_repo_unless_this_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    linked = _worktree(repo, "feature", branch="feature/one")

    assert str(repo.resolve()) in project_match_keys(linked)
    assert str(repo.resolve()) not in project_match_keys(linked, this_worktree=True)
    assert str(linked.resolve()) in project_match_keys(linked, this_worktree=True)


def test_sessions_from_sibling_worktrees_share_one_project(
    home: Path, layout: Layout, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    linked = _worktree(repo, "dat2-622", branch="feature/dat2-622")
    unrelated = _repo(tmp_path, "other")
    main_session = _claude_session(home, repo, "main", "deploy the pipeline", branch="develop")
    wt_session = _cursor_session(home, linked, "wt", "propose field level updates")
    _claude_session(home, unrelated, "elsewhere", "deploy the pipeline", branch="main")

    from_main = list_sessions(layout, cwd=repo)
    assert {hit.source_path for hit in from_main} == {str(main_session), str(wt_session)}
    from_worktree = list_sessions(layout, cwd=linked)
    assert {hit.source_path for hit in from_worktree} == {str(main_session), str(wt_session)}

    only_here = list_sessions(layout, cwd=linked, this_worktree=True)
    assert [hit.source_path for hit in only_here] == [str(wt_session)]
    only_main = list_sessions(layout, cwd=repo, this_worktree=True)
    assert [hit.source_path for hit in only_main] == [str(main_session)]

    found = search(layout, "field level", cwd=repo)
    assert [hit.source_path for hit in found] == [str(wt_session)]
    assert found[0].repo_root == str(repo.resolve())
    assert found[0].git_root == str(linked.resolve())
    assert found[0].branch == "feature/dat2-622"

    commands = list_commands(layout, cwd=linked)
    assert [item.command for item in commands] == ["pytest tests/test_main.py"]
    assert list_commands(layout, cwd=linked, this_worktree=True) == []


def test_branch_comes_from_transcript_or_worktree_head(home: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    linked = _worktree(repo, "feature", branch="feature/one")
    detached = _worktree(repo, "review", branch=None)

    claude = parse_transcript(
        AgentName.claude,
        _claude_session(home, repo, "main", "hello", branch="fix/thing"),
    )
    assert claude.branch == "fix/thing"
    assert claude.repo_root == str(repo.resolve())

    codex_path = home / ".codex" / "sessions" / "2026" / "09" / "01" / "rollout.jsonl"
    _write_jsonl(
        codex_path,
        [
            {
                "timestamp": "2026-09-01T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "abc",
                    "cwd": str(repo.resolve()),
                    "git": {"branch": "codex-branch", "commit_hash": "deadbeef"},
                },
            },
            {
                "timestamp": "2026-09-01T10:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "hi"},
            },
        ],
    )
    assert parse_transcript(AgentName.codex, codex_path).branch == "codex-branch"

    in_main = parse_transcript(AgentName.cursor, _cursor_session(home, repo, "a", "hi"))
    assert in_main.branch is None, "main checkout HEAD moves too often to label old chats"
    in_linked = parse_transcript(AgentName.cursor, _cursor_session(home, linked, "b", "hi"))
    assert in_linked.branch == "feature/one"
    in_detached = parse_transcript(AgentName.cursor, _cursor_session(home, detached, "c", "hi"))
    assert in_detached.branch is None
    assert in_detached.repo_root == str(repo.resolve())


def test_branch_filter_accepts_globs(home: Path, layout: Layout, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    one = _claude_session(home, repo, "one", "first", branch="feature/dat2-622")
    two = _claude_session(home, repo, "two", "second", branch="feature/dat2-654")
    three = _claude_session(home, repo, "three", "third", branch="develop")

    assert {hit.source_path for hit in list_sessions(layout, cwd=repo, branch="feature/*")} == {
        str(one),
        str(two),
    }
    assert [hit.source_path for hit in list_sessions(layout, cwd=repo, branch="develop")] == [
        str(three)
    ]
    assert search(layout, "first", cwd=repo, branch="feature/dat2-62?")[0].source_path == str(one)
    assert search(layout, "first", cwd=repo, branch="develop") == []
    assert [
        item.command for item in list_commands(layout, cwd=repo, branch="feature/dat2-654")
    ] == ["pytest tests/test_two.py"]


def test_list_worktrees_covers_registered_and_removed_checkouts(
    home: Path, layout: Layout, tmp_path: Path
) -> None:
    repo = _repo(tmp_path, branch="develop")
    linked = _worktree(repo, "dat2-622", branch="feature/dat2-622")
    _worktree(repo, "review", branch=None)
    quiet = _worktree(repo, "quiet", branch="feature/quiet")
    _claude_session(home, repo, "main", "deploy", branch="develop")
    _cursor_session(home, linked, "wt", "updates")
    _cursor_session(home, linked, "wt2", "more updates")

    rows = list_worktrees(layout, cwd=linked)
    by_name = {row.name: row for row in rows}
    assert [row.name for row in rows][0] == "app"
    assert set(by_name) == {"app", "app-dat2-622", "app-review", "app-quiet"}
    assert by_name["app"].branch == "develop"
    assert by_name["app"].sessions_by_agent == {"claude": 1}
    assert by_name["app-dat2-622"].sessions_by_agent == {"cursor": 2}
    assert by_name["app-dat2-622"].branch == "feature/dat2-622"
    assert by_name["app-review"].branch is None
    assert by_name["app-review"].sessions_by_agent == {}
    assert by_name["app-quiet"].last_activity is None
    assert all(row.repo_root == str(repo.resolve()) for row in rows)

    # A pruned worktree disappears from the table; its chats stay in the index.
    shutil.rmtree(quiet)
    (repo / ".git" / "worktrees" / "quiet" / "gitdir").unlink()
    rows = list_worktrees(layout, cwd=repo)
    assert "app-quiet" not in {row.name for row in rows}
    assert all(row.exists for row in rows)


def test_history_worktrees_cli(home: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path, branch="develop")
    linked = _worktree(repo, "dat2-622", branch="feature/dat2-622")
    _claude_session(home, repo, "main", "deploy", branch="develop")
    _cursor_session(home, linked, "wt", "updates")

    result = runner.invoke(app, ["history", "worktrees", "--cwd", str(repo)])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == f"repo\t{repo.resolve()}"
    assert lines[1].startswith("app\tdevelop\t2026-09-01T10:00:00Z\tclaude 1")
    assert lines[2].startswith("app-dat2-622\tfeature/dat2-622\t")
    assert lines[2].endswith("cursor 1")

    as_json = runner.invoke(app, ["history", "worktrees", "--cwd", str(linked), "--format", "json"])
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.stdout)
    assert payload["schema_version"] == 1
    assert payload["repo_root"] == str(repo.resolve())
    assert [item["name"] for item in payload["worktrees"]] == ["app", "app-dat2-622"]
    assert payload["worktrees"][1]["sessions_by_agent"] == {"cursor": 1}

    outside = runner.invoke(app, ["history", "worktrees", "--cwd", str(tmp_path)])
    assert outside.exit_code == 1
    assert "not inside a git repository" in outside.output


def test_history_list_prints_worktree_and_branch(home: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    linked = _worktree(repo, "dat2-622", branch="feature/dat2-622")
    _claude_session(home, repo, "main", "deploy", branch="develop")
    _cursor_session(home, linked, "wt", "updates")
    subdir = repo / "pipelines"
    subdir.mkdir()
    _cursor_session(home, subdir, "sub", "nested")

    listed = runner.invoke(app, ["history", "list", "--cwd", str(repo)])
    assert listed.exit_code == 0, listed.output
    locations = {line.split("\t")[2] for line in listed.stdout.splitlines() if "\t" in line}
    assert locations == {"app@develop", "app-dat2-622@feature/dat2-622", "app/pipelines"}

    everything = runner.invoke(app, ["history", "list", "--all", "--format", "json"])
    assert everything.exit_code == 0, everything.output
    sessions = {item["worktree"]: item for item in json.loads(everything.stdout)["sessions"]}
    assert sessions["app-dat2-622"]["branch"] == "feature/dat2-622"
    assert sessions["app-dat2-622"]["repo_root"] == str(repo.resolve())
    assert sessions["app/pipelines"]["project_cwd"] == str(subdir.resolve())

    narrowed = runner.invoke(
        app, ["history", "list", "--cwd", str(linked), "--this-worktree", "--branch", "feature/*"]
    )
    assert narrowed.exit_code == 0, narrowed.output
    assert narrowed.stdout.count("\n") == 1
    assert "app-dat2-622@feature/dat2-622" in narrowed.stdout


def test_schema_bump_backfills_every_project_not_just_the_queried_one(
    home: Path, layout: Layout, tmp_path: Path
) -> None:
    repo_a = _repo(tmp_path, "alpha")
    linked_a = _worktree(repo_a, "feature", branch="feature/a")
    repo_b = _repo(tmp_path, "beta")
    main_a = _claude_session(home, repo_a, "main", "alpha main", branch="develop")
    wt_a = _cursor_session(home, linked_a, "wt", "alpha worktree")
    _claude_session(home, repo_b, "b", "beta main", branch="main")

    list_sessions(layout, all_projects=True)
    connection = connect(layout.history_db)
    connection.execute("PRAGMA user_version = 5")
    connection.execute("UPDATE sessions SET repo_root = NULL, branch = NULL")
    connection.commit()
    connection.close()

    # First query after the upgrade is scoped to an unrelated repo.
    assert len(list_sessions(layout, cwd=repo_b)) == 1

    # The other repo must still have been backfilled, so its worktree chat is found.
    assert {hit.source_path for hit in list_sessions(layout, cwd=repo_a)} == {
        str(main_a),
        str(wt_a),
    }
    assert [hit.source_path for hit in list_sessions(layout, cwd=repo_a, branch="develop")] == [
        str(main_a)
    ]


def test_worktree_outside_sibling_layout_uses_one_name_everywhere(
    home: Path, tmp_path: Path
) -> None:
    repo = _repo(tmp_path, "app")
    elsewhere = tmp_path / "scratch" / "wt"
    elsewhere.parent.mkdir()
    admin = repo / ".git" / "worktrees" / "wt"
    admin.mkdir(parents=True)
    elsewhere.mkdir()
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{elsewhere / '.git'}\n", encoding="utf-8")
    (admin / "HEAD").write_text("ref: refs/heads/feature/far\n", encoding="utf-8")
    (elsewhere / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    _cursor_session(home, elsewhere, "far", "far away")

    listed = runner.invoke(app, ["history", "list", "--cwd", str(repo), "--format", "json"])
    assert listed.exit_code == 0, listed.output
    [session] = json.loads(listed.stdout)["sessions"]
    table = runner.invoke(app, ["history", "worktrees", "--cwd", str(repo), "--format", "json"])
    assert table.exit_code == 0, table.output
    rows = json.loads(table.stdout)["worktrees"]
    [far_row] = [row for row in rows if row["path"] == str(elsewhere.resolve())]
    assert session["worktree"] == far_row["name"] == str(elsewhere.resolve())


def test_project_scope_resolves_each_directory_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    scope = project_scope(repo)
    calls: list[str] = []
    original = Path.resolve

    def counting_resolve(self: Path, strict: bool = False) -> Path:
        calls.append(str(self))
        return original(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", counting_resolve)
    elsewhere = str(tmp_path / "elsewhere")
    records = [
        SessionRecord(
            agent="cursor",
            source_path=f"/x/{index}.jsonl",
            mtime_ns=0,
            project_cwd=elsewhere,
            git_root=None,
            started_at=None,
            title="",
            body="",
            commands=(),
        )
        for index in range(50)
    ]
    assert not any(scope.contains(record) for record in records)
    assert calls == [elsewhere]
    inside = SessionRecord(
        agent="cursor",
        source_path="/x/in.jsonl",
        mtime_ns=0,
        project_cwd=str(repo / "pipelines"),
        git_root=None,
        started_at=None,
        title="",
        body="",
        commands=(),
    )
    assert scope.contains(inside)
    assert calls == [elsewhere], "a plain prefix match needs no filesystem access"
