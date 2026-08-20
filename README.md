# agentrecall

Keep skills, global instructions, a **small permission policy**, and searchable project history in one place: `~/.agents`.

The CLI is `agentrecall` (previously `dotagents`). Cursor, Codex, and OpenCode already read `~/.agents`; Claude Code does not, so this CLI links only where a tool cannot see it. Permissions are **translated from a small YAML policy**, not copied from each tool's approval history.

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

macOS and Linux. Python 3.11+. `uv` on `PATH`.

```bash
uv tool install git+https://github.com/bluearpit/agentrecall.git@v0.1.0
# or latest from main:
# uv tool install git+https://github.com/bluearpit/agentrecall.git
agentrecall skills --apply
agentrecall permissions init --apply
agentrecall permissions --apply
```

That puts `agentrecall` on `PATH`, links the bundled `search-project-history` skill into `~/.agents/skills` (and into Claude Code), and grants `~/.agents/history` to Claude, Codex, and OpenCode so the search index can update. Cursor, Codex, and OpenCode already read `~/.agents/skills`.

Then, in a project, build the index once:

```bash
agentrecall history reindex --cwd .
```

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
agentrecall skills              # dry-run
agentrecall skills --apply      # link ~/.agents/skills into Claude Code
agentrecall skills --apply adopt
agentrecall instructions --apply
agentrecall project --apply     # CLAUDE.md -> @AGENTS.md
agentrecall history reindex --cwd .
agentrecall history search "the decision about X" --cwd .
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

## Layout

```
~/.agents/
  AGENTS.md                 # global instructions (source of truth)
  skills/<name>/SKILL.md    # user skills
  permissions.yaml          # portable allow/deny policy
  history/index.sqlite      # search index only; not full transcripts
```

| Agent | Skills | Global instructions | Transcripts (search) |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/skills` (needs link) | `~/.claude/CLAUDE.md` | `~/.claude/projects/**/*.jsonl` |
| Cursor | reads `~/.agents/skills` | User Rules in the UI | `~/.cursor/projects/**/agent-transcripts/**/*.jsonl` |
| Codex | reads `~/.agents/skills` | `~/.codex/AGENTS.md` | `~/.codex/sessions/**/*.jsonl` |
| OpenCode | reads `~/.agents/skills` | — | not indexed |

`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, and `XDG_CONFIG_HOME` are honored.

History is keyed by project directory (and git root when present). It is **not** stored in the git repo. Do not commit `~/.agents/history/`. Searching chats is not the same as resuming a Claude session inside Cursor.

The bundled skill `search-project-history` is installed into `~/.agents/skills` on `agentrecall skills --apply`, then linked into Claude Code like any other skill. Agents should run `agentrecall history search "<query>" --cwd .` instead of scraping transcript files themselves.

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

## Related tools

- [`npx skills`](https://github.com/vercel-labs/skills) — install skills from GitHub into agent directories
- [cc_transcript_viewer](https://github.com/tim-hua-01/cc_transcript_viewer) — GUI for the same local JSONL files

## License

MIT
