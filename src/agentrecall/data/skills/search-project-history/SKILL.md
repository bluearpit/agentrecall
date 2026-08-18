---
name: search-project-history
description: >-
  Search local Cursor, Claude Code, and Codex chats for this project, including
  dated session lists and shell commands (pytest, curl, git). Use when the user
  asks about previous conversations, past decisions, what we did last time,
  remind me, we already discussed, project history, chats since a date, last
  week, which agent, commands we ran, how we tested or curled something, smoke
  tests, or work that may have happened in another coding agent.
---

# Search project history

Search local chats from Cursor, Claude Code, and Codex for the current project. Transcripts stay in each tool's native store. This skill only searches them.

## When to use

- The user asks what was decided or done in an earlier chat
- The user says remind me, we already discussed this, or what did we do last time
- The user mentions work that may have happened in Claude Code, Cursor, or Codex
- You need prior context for this repo and do not have it in the current conversation
- The user asks which commands were run, how we tested, or which curl/git was used
- The user wants chats or commands since a date, last week, or from a named agent

## Command

`agentrecall` must be on `PATH` (`uv tool install git+https://github.com/bluearpit/agentrecall.git` or `uv tool install -e .` from a checkout).

If `agentrecall` is missing, tell the user to install it and stop. Do not invent transcript paths.

Default to the current project:

```bash
agentrecall history search "<query>" --cwd .
```

Reindex first only when search says the index is missing or looks stale:

```bash
agentrecall history reindex --cwd .
agentrecall history search "<query>" --cwd .
```

Optional flags:

- `--agent claude|cursor|codex` to limit the source
- `--all` to search every project (ask before using)
- `agentrecall history list --cwd .` for recent sessions without a query
- `agentrecall history list --cwd . --since 2026-08-01` to bound by date (`--until` too)
- `agentrecall history commands --cwd .` for shell commands indexed from those chats
- `agentrecall history commands --cwd . --kind test` or `--kind http` (also `git`, `python`, `docker`, `other`)

## How to answer

- Summarize hits: agent, date, title, and a short snippet
- Quote only the lines that answer the question
- Do not dump whole transcripts into context
- Do not claim you can resume a Claude session inside Cursor or Codex
- History is local. Never commit `~/.agents/history/`
