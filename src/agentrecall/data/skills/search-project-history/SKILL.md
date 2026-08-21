---
name: search-project-history
description: >-
  Search local Cursor, Claude Code, and Codex chats, including dated session
  lists and shell commands (pytest, curl, git). Defaults to the current
  project (`--cwd .`). Use `--cwd <path>` for another repo, or `--all` for
  every project (ask first). Use when the user asks about previous
  conversations, past decisions, what we did last time, remind me, we already
  discussed, project history, chats since a date, last week, which agent,
  commands we ran, how we tested or curled something, smoke tests, or work
  that may have happened in another coding agent.
---

# Search project history

Search local chats from Cursor, Claude Code, and Codex. Transcripts stay in each tool's native store. This skill only searches them.

## When to use

- The user asks what was decided or done in an earlier chat
- The user says remind me, we already discussed this, or what did we do last time
- The user mentions work that may have happened in Claude Code, Cursor, or Codex
- You need prior context for this repo and do not have it in the current conversation
- The user asks which commands were run, how we tested, or which curl/git was used
- The user wants chats or commands since a date, last week, or from a named agent

## Command

`agentrecall` must be on `PATH` (`uv tool install git+https://github.com/bluearpit/agentrecall.git@v0.2.0` or `uv tool install -e .` from a checkout).

If `agentrecall` is missing, tell the user to install it and stop. Do not invent transcript paths.

```bash
uv tool install git+https://github.com/bluearpit/agentrecall.git@v0.2.0
```

If the CLI prints that a newer version is available, ask the user before upgrading. Do not run `upgrade --apply` on your own.

```bash
agentrecall upgrade
agentrecall upgrade --apply
```

The history index is `~/.agents/history/index.sqlite`. That path is outside the project workspace. After `agentrecall permissions --apply`, Claude, Codex, and OpenCode get write access to it; Cursor CLI allowlists `agentrecall` but still has no extra-root mapping. If a sandbox blocks the command, request the permission once and continue. Do not scrape transcript files yourself.

Default to the current project:

```bash
agentrecall history search "<query>" --cwd .
```

Another repo:

```bash
agentrecall history search "<query>" --cwd /path/to/project
```

Every project (ask before using; results mix unrelated repos):

```bash
agentrecall history search "<query>" --all
```

After picking a hit, open matching turns instead of scraping the JSONL:

```bash
agentrecall history show <source_path> --grep "<term>"
agentrecall history show <source_path> --grep "<term>" --context 1
```

Reindex first only when search says the index is missing or looks stale. Match the same scope you will search (`--cwd` or `--all`):

```bash
agentrecall history reindex --cwd .
agentrecall history search "<query>" --cwd .
```

Optional flags:

- `--agent claude|cursor|codex` to limit the source
- `--sort relevance` (default) or `--sort recent`
- `agentrecall history list --cwd .` for recent sessions without a query
- `agentrecall history list --cwd . --since 2026-08-01` to bound by date (`--until` too)
- `agentrecall history commands --cwd .` for shell commands indexed from those chats
- `agentrecall history commands --cwd . --kind test` or `--kind http` (also `git`, `python`, `docker`, `other`)
- `--all` also works on `reindex`, `list`, and `commands`

## How to answer

- Summarize hits: agent, date, title, project path, and a short snippet
- Run `history show --grep` on a candidate path to verify; quote only the lines that answer the question
- Do not dump whole transcripts into context
- Do not claim you can resume a Claude session inside Cursor or Codex
- History is local. Never commit `~/.agents/history/`
