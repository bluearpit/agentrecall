---
name: search-project-history
description: Search prior Cursor, Claude Code, and Codex chats for this project. Use when the user asks about previous conversations, past decisions, what we did last time, or project history across coding agents.
---

# Search project history

Search local chats from Cursor, Claude Code, and Codex for the current project. Transcripts stay in each tool's native store. This skill only searches them.

## When to use

- The user asks what was decided or done in an earlier chat
- The user mentions work that may have happened in Claude Code, Cursor, or Codex
- You need prior context for this repo and do not have it in the current conversation

## Command

`dotagents` must be on `PATH` (`uv tool install dotagents` or `uv tool install -e .` from a checkout).

If `dotagents` is missing, tell the user to install it and stop. Do not invent transcript paths.

Default to the current project:

```bash
dotagents history search "<query>" --cwd .
```

Reindex first only when search says the index is missing or looks stale:

```bash
dotagents history reindex --cwd .
dotagents history search "<query>" --cwd .
```

Optional flags:

- `--agent claude|cursor|codex` to limit the source
- `--all` to search every project (ask before using)
- `dotagents history list --cwd .` for recent sessions without a query

## How to answer

- Summarize hits: agent, date, title, and a short snippet
- Quote only the lines that answer the question
- Do not dump whole transcripts into context
- Do not claim you can resume a Claude session inside Cursor or Codex
- History is local. Never commit `~/.agents/history/`
