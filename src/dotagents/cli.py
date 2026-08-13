"""Typer CLI for dotagents."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from dotagents import __version__
from dotagents.history import list_sessions, reindex, search
from dotagents.instructions import sync_instructions
from dotagents.layout import Layout
from dotagents.linking import LinkMode
from dotagents.permissions import (
    git_toplevel,
    init_policy,
    project_permissions_file,
    sync_permissions,
)
from dotagents.project import sync_project
from dotagents.skills import adopt_plan, render_and_apply, sync_skills
from dotagents.status import status_lines

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Sync skills, instructions, permissions, and project history from ~/.agents.",
)
skills_app = typer.Typer(no_args_is_help=False, help="Link skills from ~/.agents/skills.")
history_app = typer.Typer(no_args_is_help=True, help="Search local agent transcripts.")
permissions_app = typer.Typer(no_args_is_help=False, help="Translate a small permission policy.")
app.add_typer(skills_app, name="skills")
app.add_typer(history_app, name="history")
app.add_typer(permissions_app, name="permissions")


def _layout() -> Layout:
    return Layout.from_environ()


def _echo_lines(lines: list[str]) -> None:
    for line in lines:
        typer.echo(line)


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
    return


@app.command("status")
def status_cmd() -> None:
    """Show canonical ~/.agents state and per-agent coverage."""
    _echo_lines(status_lines(_layout()))


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


@app.command("instructions")
def instructions_cmd(
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
    dry_run = not apply
    if dry_run:
        typer.echo("dry-run (pass --apply to write)")
    lines, conflicts = sync_instructions(_layout(), dry_run=dry_run, mode=link_mode)
    _echo_lines(lines)
    _fail_on_conflicts(conflicts)


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


@history_app.command("reindex")
def history_reindex(
    cwd: Annotated[
        Path | None,
        typer.Option("--cwd", help="Project directory. Defaults to the current directory."),
    ] = None,
    all_projects: Annotated[
        bool,
        typer.Option("--all", help="Index transcripts for every project."),
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
        typer.Option("--cwd", help="Project directory. Defaults to the current directory."),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Limit to claude, cursor, or codex."),
    ] = None,
    all_projects: Annotated[
        bool,
        typer.Option("--all", help="Search across every project."),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
) -> None:
    """Search indexed chats for this project."""
    hits = search(
        _layout(),
        query,
        cwd=_optional_cwd(cwd),
        agent=agent,
        all_projects=all_projects,
        limit=limit,
    )
    if not hits:
        typer.echo("no matches")
        return
    for hit in hits:
        typer.echo(f"{hit.agent}\t{hit.started_at or '-'}\t{hit.project_cwd or '-'}")
        typer.echo(f"  {hit.title}")
        typer.echo(f"  {hit.source_path}")
        typer.echo(f"  {hit.snippet}")


@history_app.command("list")
def history_list(
    cwd: Annotated[
        Path | None,
        typer.Option("--cwd", help="Project directory. Defaults to the current directory."),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Limit to claude, cursor, or codex."),
    ] = None,
    all_projects: Annotated[
        bool,
        typer.Option("--all", help="List sessions for every project."),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 20,
) -> None:
    """List recent sessions for this project."""
    hits = list_sessions(
        _layout(),
        cwd=_optional_cwd(cwd),
        agent=agent,
        all_projects=all_projects,
        limit=limit,
    )
    if not hits:
        typer.echo("no sessions")
        return
    for hit in hits:
        typer.echo(f"{hit.agent}\t{hit.started_at or '-'}\t{hit.project_cwd or '-'}\t{hit.title}")
