"""Typer CLI for agentrecall."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from agentrecall import __version__
from agentrecall.history import (
    SEARCH_SORTS,
    SearchHit,
    checkout_name,
    format_turn,
    list_commands,
    list_sessions,
    list_worktrees,
    load_turns,
    parse_history_bound,
    reindex,
    search,
    select_turns,
)
from agentrecall.instructions import (
    LINKABLE_AGENTS,
    InstructionTarget,
    init_instructions,
    link_instructions,
    render_instructions,
    sync_instructions,
)
from agentrecall.layout import AgentName, Layout
from agentrecall.linking import LinkMode
from agentrecall.permissions import (
    git_toplevel,
    init_policy,
    project_permissions_file,
    sync_permissions,
)
from agentrecall.project import sync_project
from agentrecall.skills import adopt_plan, render_and_apply, sync_skills
from agentrecall.status import status_lines
from agentrecall.upgrade import (
    apply_upgrade,
    fetch_latest_version,
    is_newer,
    notice_if_outdated,
    upgrade_plan,
)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Portable agent skills, instructions, permissions, and history in ~/.agents.",
)
skills_app = typer.Typer(no_args_is_help=False, help="Link skills from ~/.agents/skills.")
history_app = typer.Typer(no_args_is_help=True, help="Search local agent transcripts.")
permissions_app = typer.Typer(no_args_is_help=False, help="Translate a small permission policy.")
instructions_app = typer.Typer(
    no_args_is_help=False,
    help="Create canonical instructions and activate their adapters.",
)
app.add_typer(skills_app, name="skills")
app.add_typer(history_app, name="history")
app.add_typer(permissions_app, name="permissions")
app.add_typer(instructions_app, name="instructions")


def _layout() -> Layout:
    return Layout.from_environ()


def _echo_lines(lines: list[str]) -> None:
    for line in lines:
        typer.echo(line)


def _history_hit_json(hit: SearchHit, *, include_snippet: bool) -> dict[str, object]:
    item: dict[str, object] = {
        "agent": hit.agent,
        "session_id": hit.session_id,
        "source_path": hit.source_path,
        "project_cwd": hit.project_cwd,
        "repo_root": hit.repo_root,
        "worktree": _worktree_label(hit),
        "branch": hit.branch,
        "name": hit.name,
        "title": hit.title,
        "started_at": hit.started_at,
        "updated_at": hit.updated_at,
        "capabilities": {"searchable": hit.searchable, "resumable": hit.resumable},
    }
    if include_snippet:
        item["snippet"] = hit.snippet
    return item


def _worktree_label(hit: SearchHit) -> str | None:
    """Checkout directory name plus any subdirectory the session started in."""
    if hit.project_cwd is None:
        return None
    base = Path(hit.git_root or hit.project_cwd)
    label = checkout_name(base, None if hit.repo_root is None else Path(hit.repo_root))
    try:
        inside = Path(hit.project_cwd).relative_to(base)
    except ValueError:
        return label
    return label if str(inside) == "." else f"{label}/{inside.as_posix()}"


def _location(hit: SearchHit, *, all_projects: bool) -> str:
    """Text-output location: ``worktree@branch`` inside one repo, full path with ``--all``."""
    if hit.project_cwd is None:
        return "-"
    base = hit.project_cwd if all_projects else (_worktree_label(hit) or hit.project_cwd)
    return f"{base}@{hit.branch}" if hit.branch else base


def _history_format(value: str) -> str:
    if value not in {"text", "json"}:
        typer.echo(f"Error: unknown format {value!r}", err=True)
        raise typer.Exit(code=1)
    return value


def _fail_on_conflicts(conflicts: int) -> None:
    if conflicts > 0:
        typer.echo(f"Error: {conflicts} conflict(s); nothing else blocked was written.", err=True)
        raise typer.Exit(code=1)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    if ctx.invoked_subcommand == "upgrade":
        return
    notice = notice_if_outdated(_layout())
    if notice is not None:
        typer.echo(notice, err=True)


@app.command("status")
def status_cmd(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show every skill and its link state."),
    ] = False,
) -> None:
    """Show canonical ~/.agents state and per-agent coverage."""
    _echo_lines(status_lines(_layout(), verbose=verbose))


@app.command("upgrade")
def upgrade_cmd(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Install that version from PyPI. Default is dry-run."),
    ] = False,
) -> None:
    """Check PyPI for a newer release and optionally install it."""
    latest = fetch_latest_version()
    lines, failed = upgrade_plan(latest=latest)
    if not apply:
        typer.echo("dry-run (pass --apply to write)")
        _echo_lines(lines)
        if failed:
            raise typer.Exit(code=1)
        return
    if failed:
        _echo_lines(lines)
        raise typer.Exit(code=1)
    if latest is None or not is_newer(latest, __version__):
        _echo_lines(lines)
        return
    try:
        typer.echo(apply_upgrade(latest))
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@skills_app.callback(invoke_without_command=True)
def skills_root(
    ctx: typer.Context,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write links. Default is dry-run."),
    ] = False,
    link_mode: Annotated[
        LinkMode,
        typer.Option("--link-mode", help="Skill folders always symlink. Kept for CLI consistency."),
    ] = LinkMode.auto,
) -> None:
    """Link ~/.agents/skills into Claude Code (dry-run unless --apply)."""
    ctx.obj = {"apply": apply, "link_mode": link_mode}
    if ctx.invoked_subcommand is not None:
        return
    if link_mode is LinkMode.hardlink:
        typer.echo("Note: skill folders cannot be hardlinked; using directory symlinks.", err=True)
    dry_run = not apply
    lines, conflicts = sync_skills(_layout(), dry_run=dry_run)
    if dry_run:
        lines.insert(0, "dry-run (pass --apply to write)")
    _echo_lines(lines)
    _fail_on_conflicts(conflicts)


@skills_app.command("adopt")
def skills_adopt(ctx: typer.Context) -> None:
    """Link Claude-only skills into ~/.agents/skills so other agents can see them."""
    apply = bool(ctx.obj and ctx.obj.get("apply"))
    dry_run = not apply
    items = adopt_plan(_layout())
    if not items:
        typer.echo("no Claude-only skills to adopt")
        return
    if dry_run:
        typer.echo("dry-run (pass --apply to write)")
    lines, conflicts = render_and_apply(items, dry_run=dry_run)
    _echo_lines(lines)
    _fail_on_conflicts(conflicts)


@instructions_app.callback(invoke_without_command=True)
def instructions_root(
    ctx: typer.Context,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write files and links. Default is dry-run."),
    ] = False,
    link_mode: Annotated[
        LinkMode,
        typer.Option("--link-mode", help="auto (symlink then hardlink), symlink, or hardlink."),
    ] = LinkMode.auto,
) -> None:
    """Create ~/.agents/AGENTS.md and link Claude/Codex instruction files to it."""
    ctx.obj = {"apply": apply, "link_mode": link_mode}
    if ctx.invoked_subcommand is not None:
        return
    dry_run = not apply
    if dry_run:
        typer.echo("dry-run (pass --apply to write)")
    lines, conflicts = sync_instructions(_layout(), dry_run=dry_run, mode=link_mode)
    _echo_lines(lines)
    _fail_on_conflicts(conflicts)


@instructions_app.command("init")
def instructions_init(
    ctx: typer.Context,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write ~/.agents/AGENTS.md. Default is dry-run."),
    ] = False,
) -> None:
    """Create the canonical AGENTS.md without linking it anywhere."""
    apply = apply or bool(ctx.obj and ctx.obj.get("apply"))
    dry_run = not apply
    if dry_run:
        typer.echo("dry-run (pass --apply to write)")
    _echo_lines(init_instructions(_layout(), dry_run=dry_run))


@instructions_app.command("link")
def instructions_link(
    ctx: typer.Context,
    agent: Annotated[
        InstructionTarget | None,
        typer.Option("--agent", help="Link only claude or codex."),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write links. Default is dry-run."),
    ] = False,
    link_mode: Annotated[
        LinkMode | None,
        typer.Option("--link-mode", help="auto (symlink then hardlink), symlink, or hardlink."),
    ] = None,
) -> None:
    """Link canonical instructions into Claude and/or Codex."""
    apply = apply or bool(ctx.obj and ctx.obj.get("apply"))
    parent_mode = ctx.obj.get("link_mode") if ctx.obj else None
    mode = link_mode or parent_mode or LinkMode.auto
    agents = LINKABLE_AGENTS if agent is None else (AgentName(agent.value),)
    dry_run = not apply
    if dry_run:
        typer.echo("dry-run (pass --apply to write)")
    try:
        lines, conflicts = link_instructions(
            _layout(),
            dry_run=dry_run,
            mode=mode,
            agents=agents,
        )
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _echo_lines(lines)
    _fail_on_conflicts(conflicts)


@instructions_app.command("show")
def instructions_show() -> None:
    """Print canonical instructions for pasting into an agent's UI."""
    try:
        typer.echo(render_instructions(_layout()), nl=False)
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("project")
def project_cmd(
    path: Annotated[
        Path | None,
        typer.Argument(help="Project root. Defaults to the current directory."),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write CLAUDE.md. Default is dry-run."),
    ] = False,
) -> None:
    """Create a CLAUDE.md shim that imports AGENTS.md."""
    root = (path or Path.cwd()).resolve()
    dry_run = not apply
    if dry_run:
        typer.echo("dry-run (pass --apply to write)")
    line, failed = sync_project(root, dry_run=dry_run)
    typer.echo(line)
    if failed:
        raise typer.Exit(code=1)


@permissions_app.callback(invoke_without_command=True)
def permissions_root(
    ctx: typer.Context,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write native permission files. Default is dry-run."),
    ] = False,
    cwd: Annotated[
        Path | None,
        typer.Option("--cwd", help="Project directory used to find .agents/permissions.yaml."),
    ] = None,
) -> None:
    """Show or apply the portable permission policy."""
    ctx.obj = {"apply": apply, "cwd": cwd}
    if ctx.invoked_subcommand is not None:
        return
    dry_run = not apply
    try:
        lines, failed = sync_permissions(
            _layout(),
            cwd=_optional_cwd(cwd),
            dry_run=dry_run,
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _echo_lines(lines)
    if failed:
        raise typer.Exit(code=1)


@permissions_app.command("init")
def permissions_init(
    ctx: typer.Context,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write the file. Default is dry-run."),
    ] = False,
    cwd: Annotated[
        Path | None,
        typer.Option("--cwd", help="Write .agents/permissions.yaml in this project."),
    ] = None,
) -> None:
    """Write a starter permissions.yaml (user or project)."""
    apply = apply or bool(ctx.obj and ctx.obj.get("apply"))
    option_cwd = cwd
    if option_cwd is None and ctx.obj and ctx.obj.get("cwd") is not None:
        option_cwd = ctx.obj["cwd"]
    dry_run = not apply
    if option_cwd is not None:
        root = git_toplevel(option_cwd) or option_cwd.resolve()
        path = project_permissions_file(root)
    else:
        path = _layout().permissions_file
    if dry_run:
        typer.echo("dry-run (pass --apply to write)")
    typer.echo(init_policy(path, dry_run=dry_run))


def _optional_cwd(cwd: Path | None) -> Path | None:
    if cwd is None:
        return Path.cwd()
    return cwd


def _time_bounds(
    since: str | None,
    until: str | None,
) -> tuple[datetime | None, datetime | None]:
    try:
        since_at = parse_history_bound(since, end_of_day=False) if since is not None else None
        until_at = parse_history_bound(until, end_of_day=True) if until is not None else None
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    return since_at, until_at


def _compact_command(command: str) -> str:
    compact = re.sub(r"\s+", " ", command).strip()
    if len(compact) <= 200:
        return compact
    return compact[:197] + "..."


@history_app.command("reindex")
def history_reindex(
    cwd: Annotated[
        Path | None,
        typer.Option("--cwd", help="Project to index. Defaults to the current directory."),
    ] = None,
    all_projects: Annotated[
        bool,
        typer.Option("--all", help="Index every project instead of --cwd."),
    ] = False,
) -> None:
    """Scan native JSONL transcripts into ~/.agents/history/index.sqlite."""
    indexed, skipped = reindex(
        _layout(),
        cwd=_optional_cwd(cwd),
        all_projects=all_projects,
    )
    typer.echo(f"indexed {indexed}  skipped {skipped}")


@history_app.command("search")
def history_search(
    query: Annotated[str, typer.Argument(help="Full-text query.")],
    cwd: Annotated[
        Path | None,
        typer.Option("--cwd", help="Project to search. Defaults to the current directory."),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Limit to claude, cursor, codex, or pi."),
    ] = None,
    all_projects: Annotated[
        bool,
        typer.Option("--all", help="Search every project instead of --cwd."),
    ] = False,
    sort: Annotated[
        str,
        typer.Option("--sort", help="relevance (default) or recent."),
    ] = "relevance",
    branch: Annotated[
        str | None,
        typer.Option("--branch", help="Limit to a git branch; * and ? wildcards allowed."),
    ] = None,
    this_worktree: Annotated[
        bool,
        typer.Option(
            "--this-worktree",
            help="Only this checkout and its subdirectories; skip sibling worktrees.",
        ),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
    output_format: Annotated[
        str,
        typer.Option("--format", help="text (default) or json."),
    ] = "text",
) -> None:
    """Search indexed chats for a project (or every project with --all)."""
    if sort not in SEARCH_SORTS:
        typer.echo(f"Error: unknown sort {sort!r}", err=True)
        raise typer.Exit(code=1)
    _history_format(output_format)
    hits = search(
        _layout(),
        query,
        cwd=_optional_cwd(cwd),
        agent=agent,
        all_projects=all_projects,
        limit=limit,
        sort=sort,
        branch=branch,
        this_worktree=this_worktree,
    )
    if output_format == "json":
        typer.echo(
            json.dumps(
                {
                    "schema_version": 1,
                    "matches": [_history_hit_json(hit, include_snippet=True) for hit in hits],
                }
            )
        )
        return
    if not hits:
        typer.echo("no matches")
        return
    for hit in hits:
        location = _location(hit, all_projects=all_projects)
        typer.echo(f"{hit.agent}\t{hit.started_at or '-'}\t{location}")
        typer.echo(f"  {hit.title}")
        typer.echo(f"  {hit.source_path}")
        typer.echo(f"  {hit.snippet}")


@history_app.command("show")
def history_show(
    source: Annotated[
        Path,
        typer.Argument(help="Path to a native JSONL transcript."),
    ],
    grep: Annotated[
        str | None,
        typer.Option("--grep", help="Print turns containing this text."),
    ] = None,
    context: Annotated[
        int,
        typer.Option("--context", min=0, max=20, help="Turns to include around each grep hit."),
    ] = 0,
) -> None:
    """Print normalized role+text turns from a native transcript."""
    try:
        turns = load_turns(source.expanduser())
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    selected = select_turns(turns, grep=grep, context=context)
    if not selected:
        typer.echo("no matches")
        return
    for index, turn in enumerate(selected):
        if index:
            typer.echo("")
        typer.echo(format_turn(turn))


@history_app.command("list")
def history_list(
    cwd: Annotated[
        Path | None,
        typer.Option("--cwd", help="Project to list. Defaults to the current directory."),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Limit to claude, cursor, codex, or pi."),
    ] = None,
    all_projects: Annotated[
        bool,
        typer.Option("--all", help="List sessions for every project instead of --cwd."),
    ] = False,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Inclusive start date (YYYY-MM-DD or ISO timestamp)."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", help="Inclusive end date (YYYY-MM-DD or ISO timestamp)."),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", help="Limit to a git branch; * and ? wildcards allowed."),
    ] = None,
    this_worktree: Annotated[
        bool,
        typer.Option(
            "--this-worktree",
            help="Only this checkout and its subdirectories; skip sibling worktrees.",
        ),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 20,
    output_format: Annotated[
        str,
        typer.Option("--format", help="text (default) or json."),
    ] = "text",
) -> None:
    """List recent sessions for a project (or every project with --all)."""
    since_at, until_at = _time_bounds(since, until)
    _history_format(output_format)
    hits = list_sessions(
        _layout(),
        cwd=_optional_cwd(cwd),
        agent=agent,
        all_projects=all_projects,
        limit=limit,
        since=since_at,
        until=until_at,
        branch=branch,
        this_worktree=this_worktree,
    )
    if output_format == "json":
        typer.echo(
            json.dumps(
                {
                    "schema_version": 1,
                    "sessions": [_history_hit_json(hit, include_snippet=False) for hit in hits],
                }
            )
        )
        return
    if not hits:
        typer.echo("no sessions")
        return
    for hit in hits:
        location = _location(hit, all_projects=all_projects)
        typer.echo(f"{hit.agent}\t{hit.started_at or '-'}\t{location}\t{hit.title}")
        if hit.agent == AgentName.pi.value:
            typer.echo(f"  {hit.source_path}")


@history_app.command("worktrees")
def history_worktrees(
    cwd: Annotated[
        Path | None,
        typer.Option("--cwd", help="Any checkout of the repo. Defaults to the current directory."),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="text (default) or json."),
    ] = "text",
) -> None:
    """Show every worktree of this repo with its branch and indexed chats per agent."""
    _history_format(output_format)
    try:
        summaries = list_worktrees(_layout(), cwd=_optional_cwd(cwd))
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if output_format == "json":
        typer.echo(
            json.dumps(
                {
                    "schema_version": 1,
                    "repo_root": summaries[0].repo_root,
                    "worktrees": [
                        {
                            "name": item.name,
                            "path": item.path,
                            "branch": item.branch,
                            "exists": item.exists,
                            "sessions_by_agent": item.sessions_by_agent,
                            "last_activity": item.last_activity,
                        }
                        for item in summaries
                    ],
                }
            )
        )
        return
    typer.echo(f"repo\t{summaries[0].repo_root}")
    for item in summaries:
        counts = ", ".join(f"{agent} {count}" for agent, count in item.sessions_by_agent.items())
        name = item.name if item.exists else f"{item.name} (removed)"
        branch = item.branch or "detached"
        typer.echo(f"{name}\t{branch}\t{item.last_activity or '-'}\t{counts or 'no chats'}")


@history_app.command("commands")
def history_commands(
    cwd: Annotated[
        Path | None,
        typer.Option("--cwd", help="Project to list. Defaults to the current directory."),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Limit to claude, cursor, codex, or pi."),
    ] = None,
    kind: Annotated[
        str | None,
        typer.Option("--kind", help="Limit to test, http, git, python, docker, or other."),
    ] = None,
    all_projects: Annotated[
        bool,
        typer.Option("--all", help="List commands for every project instead of --cwd."),
    ] = False,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Inclusive start date (YYYY-MM-DD or ISO timestamp)."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", help="Inclusive end date (YYYY-MM-DD or ISO timestamp)."),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", help="Limit to a git branch; * and ? wildcards allowed."),
    ] = None,
    this_worktree: Annotated[
        bool,
        typer.Option(
            "--this-worktree",
            help="Only this checkout and its subdirectories; skip sibling worktrees.",
        ),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 50,
) -> None:
    """List shell commands indexed from local transcripts."""
    since_at, until_at = _time_bounds(since, until)
    try:
        hits = list_commands(
            _layout(),
            cwd=_optional_cwd(cwd),
            agent=agent,
            kind=kind,
            all_projects=all_projects,
            limit=limit,
            since=since_at,
            until=until_at,
            branch=branch,
            this_worktree=this_worktree,
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not hits:
        typer.echo("no commands")
        return
    for hit in hits:
        typer.echo(f"{hit.kind}\t{hit.agent}\t{hit.occurred_at or '-'}\t{hit.tool}")
        typer.echo(f"  {_compact_command(hit.command)}")
        if hit.purpose is not None:
            typer.echo(f"  {hit.purpose}")
