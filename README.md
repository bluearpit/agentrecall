# agentrecall

[![PyPI](https://img.shields.io/pypi/v/agentrecall-cli.svg)](https://pypi.org/project/agentrecall-cli/) [![Python](https://img.shields.io/pypi/pyversions/agentrecall-cli.svg)](https://pypi.org/project/agentrecall-cli/) [![CI](https://github.com/bluearpit/agentrecall/actions/workflows/ci.yml/badge.svg)](https://github.com/bluearpit/agentrecall/actions/workflows/ci.yml)

Agent Recall turns `~/.agents` into a portable home directory for coding agents. Keep skills, global instructions, a **small permission policy**, and searchable project history in one place, then expose each part through the adapter its agent understands.

The name includes “recall,” but transcript search is only one part of the product. The CLI is `agentrecall` (previously `dotagents`). Cursor, Codex, and OpenCode already read `~/.agents`; Claude Code does not, so this CLI links only where a tool cannot see it. Permissions are **translated from a small YAML policy**, not copied from each tool's approval history.

## What syncs

| Thing | Shared? | How |
| --- | --- | --- |
| Skills (`SKILL.md`) | Yes | Canonical `~/.agents/skills/`. Directory symlink into `~/.claude/skills/`. |
| Global instructions | Yes | Canonical `~/.agents/AGENTS.md`. File symlink (or hardlink) to `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`. |
| Project instructions | Yes, with a shim | Repo `AGENTS.md` is the source. `agentrecall project` writes `CLAUDE.md` containing `@AGENTS.md` if that file is missing. |
| Chats | Search only | Native JSONL stays put. SQLite FTS index at `~/.agents/history/index.sqlite`. |
| Permissions | Partial | Canonical `permissions.yaml`. Shell/fetch rules for every agent, plus `external_write` extra roots for Claude, Codex, and OpenCode. Cursor CLI gets allowlisted commands only. |
| MCP ids, YOLO modes, full OS sandbox profiles | No | Product-specific; not copied. |
| Cursor User Rules | No | They live in the Cursor UI, not a documented file. Paste the same text into **Customize → Rules**. |

## Why not copy?

Copying `~/.agents/skills` into every agent directory is how files go stale. On one machine, `~/.agents/skills/dev-browser` was current while `~/.claude/skills/dev-browser` was months old. `agentrecall` uses live links instead.

Skill **folders** are directory symlinks (POSIX cannot hardlink directories). Instruction **files** try a symlink first, then a hardlink with `--link-mode hardlink` or when a symlink cannot be created on the same volume.

## Install

macOS and Linux. Python 3.11+. `uv` on `PATH`. The PyPI package is [`agentrecall-cli`](https://pypi.org/project/agentrecall-cli/) (`agentrecall` is taken). That install puts the `agentrecall` command on `PATH`.

```bash
uv tool install agentrecall-cli
agentrecall skills --apply
agentrecall permissions init --apply
agentrecall permissions --apply
```

That puts `agentrecall` on `PATH`, links the bundled `search-project-history` skill into `~/.agents/skills` (and into Claude Code), and grants `~/.agents/history` to Claude, Codex, and OpenCode so the search index can update. Cursor, Codex, and OpenCode already read `~/.agents/skills`.

One-off without installing: `uvx --from agentrecall-cli agentrecall status`. From unreleased `main`: `uv tool install git+https://github.com/bluearpit/agentrecall.git`.

Then, in a project, build the index once:

```bash
agentrecall history reindex --cwd .
```

The CLI checks PyPI at most once a day (a failed check also counts) and prints a notice when a newer `agentrecall-cli` exists. The check is remembered in `~/.agents/history/update-check.json`; when that file cannot be written, the check is skipped and no command fails. It does **not** upgrade by itself. `agentrecall upgrade` is dry-run; `agentrecall upgrade --apply` installs that version from PyPI. Agents should ask before applying.

From a checkout:

```bash
uv tool install -e .
# or, for development:
uv sync --group dev
uv run agentrecall status
```

## Commands

Write commands are **dry-run unless `--apply`**.

```bash
agentrecall status
agentrecall status --verbose    # include every skill and link state
agentrecall upgrade             # dry-run
agentrecall upgrade --apply     # install that version from PyPI
agentrecall skills              # dry-run
agentrecall skills --apply      # link ~/.agents/skills into Claude Code
agentrecall skills --apply adopt
agentrecall instructions init --apply
agentrecall instructions link --apply
agentrecall instructions link --agent codex --apply
agentrecall instructions show # exact text to paste into Cursor or OpenCode
agentrecall instructions --apply # legacy combined init + link flow
agentrecall project --apply     # CLAUDE.md -> @AGENTS.md
agentrecall history reindex --cwd .
agentrecall history search "the decision about X" --cwd .
agentrecall history search "the decision about X" --cwd ~/work/other-app
agentrecall history search "the decision about X" --all
agentrecall history search "the decision about X" --cwd . --sort recent
agentrecall history show ~/.claude/projects/.../sess.jsonl --grep "the decision"
agentrecall history list --cwd .
agentrecall history list --cwd . --since 2026-08-01
agentrecall history commands --cwd .
agentrecall history commands --cwd . --kind test
agentrecall permissions init --apply
agentrecall permissions                 # dry-run mapping
agentrecall permissions --apply         # user ~/.agents/permissions.yaml -> user agent files
agentrecall permissions init --cwd . --apply
agentrecall permissions --cwd . --apply # project .agents/permissions.yaml -> project files
```

`--link-mode auto|symlink|hardlink` applies to files. Skill folders always symlink.

`instructions init` creates only the canonical file. `instructions link` activates file-based adapters for Claude Code and Codex, optionally for one `--agent`. `instructions show` prints the exact canonical text to paste into Cursor or OpenCode's UI; there are no target-specific transformations.

## Layout

```
~/.agents/
  AGENTS.md                 # global instructions (source of truth)
  skills/<name>/SKILL.md    # user skills
  permissions.yaml          # portable allow/deny policy
  history/index.sqlite      # search index only; not full transcripts
  history/update-check.json # last PyPI version check; safe to delete
```

The instruction model is one source of truth with generated or manually synchronized adapters:

```text
~/.agents/AGENTS.md        source of truth
├── ~/.claude/CLAUDE.md    symlink or hardlink adapter
├── ~/.codex/AGENTS.md     symlink or hardlink adapter
├── Cursor User Rules      paste from `instructions show`
└── OpenCode instructions  paste from `instructions show`
```

| Agent | Skills | Global instructions | Transcripts (search) |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/skills` (needs link) | `~/.claude/CLAUDE.md` | `~/.claude/projects/**/*.jsonl` |
| Cursor | reads `~/.agents/skills` | User Rules in the UI | `~/.cursor/projects/**/agent-transcripts/**/*.jsonl` |
| Codex | reads `~/.agents/skills` | `~/.codex/AGENTS.md` | `~/.codex/sessions/**/*.jsonl` |
| OpenCode | reads `~/.agents/skills` | — | not indexed |

`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, and `XDG_CONFIG_HOME` are honored.

History is keyed by project directory (and git root when present). Search, list, and commands default to the current project (`--cwd .`). Pass `--cwd /path/to/project` for one other repo, or `--all` for every indexed project. The index uses SQLite WAL mode and a busy timeout. When transcripts have not changed, searches use read-only connections; changed transcripts are parsed before a short write transaction. This allows parallel searches without turning every read into an index write.

The index is **not** stored in the git repo. Do not commit `~/.agents/history/`. Searching chats is not the same as resuming a Claude session inside Cursor.

The bundled skill `search-project-history` is installed into `~/.agents/skills` on `agentrecall skills --apply`, then linked into Claude Code like any other skill. Agents should run `agentrecall history search "<query>" --cwd .` to triage the current repo (or `--cwd <path>` / `--all` when the question is not about this repo), then `agentrecall history show <source_path> --grep "<term>"` to verify, instead of scraping transcript files themselves.

## Permissions

Write a small policy, not a dump of every command you ever approved:

```yaml
allow_shell:
  - git status
  - git diff
  - agentrecall
deny_shell:
  - git push --force
allow_fetch:
  - github.com
workspace_write: true
external_write:
  - ~/.agents/history
```

`agentrecall permissions --apply` turns that into:

| Agent | File | What is written |
| --- | --- | --- |
| Claude Code | `~/.claude/settings.json` or project `.claude/settings.json` | `Bash(git status:*)`, `WebFetch(domain:github.com)`, optional `Edit`/`Write`, `additionalDirectories`, `sandbox.filesystem.allowWrite` |
| Cursor CLI | `~/.cursor/cli-config.json` | `Shell(git status)`, `Shell(agentrecall)`, `WebFetch(github.com)` (user-global only; no extra-root mapping) |
| Codex | `~/.codex/rules/agentrecall.rules` or project `.codex/rules/agentrecall.rules` | `prefix_rule` allow/forbidden |
| Codex | `~/.codex/config.toml` or project `.codex/config.toml` | `sandbox_workspace_write.writable_roots` for `external_write` |
| OpenCode | `opencode.json` | `permission.bash` / `webfetch` / `edit` / `external_directory` |

The default policy allowlists `agentrecall` and grants `~/.agents/history` so history search can update the index without a one-off sandbox prompt. Add more paths under `external_write` if you also want `~/.agents/skills` writable.

Existing unrelated allow rules are kept. Previously generated `agentrecall` rules are replaced on the next apply (tracked in `permissions.managed.json`, not for git).

Not translated: MCP ids (`mcp__plugin_...`), one-off heredoc approvals, `bypassPermissions` / run-everything modes, Cursor's sandbox prompt for writes outside the project, or full Seatbelt/Bubblewrap profiles.

See [`examples/permissions.yaml`](examples/permissions.yaml).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, and how PRs merge.

```bash
uv sync --group dev
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest
```

CI runs the same lint and tests on Python 3.11–3.13, then builds and smoke-tests the wheel and source distribution. The `ci` job is the merge gate: it is green only when every check passes. Tests always set `HOME` to a temporary directory.

A published GitHub release also uploads to PyPI as [`agentrecall-cli`](https://pypi.org/project/agentrecall-cli/). The workflow uses Trusted Publishing (no API token). GitHub environment `pypi`; workflow `publish.yml`.

## Related tools

- [`npx skills`](https://github.com/vercel-labs/skills) — install skills from GitHub into agent directories
- [cc_transcript_viewer](https://github.com/tim-hua-01/cc_transcript_viewer) — GUI for the same local JSONL files

## License

MIT
