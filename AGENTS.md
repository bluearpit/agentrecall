# agentrecall

Python 3.11+ CLI. Source lives under `src/agentrecall`. Tests live under `tests` and must use a fake `HOME`.

- Use pathlib and explicit preconditions.
- Write commands default to dry-run; `--apply` writes.
- Translate only the small `permissions.yaml` policy; do not copy MCP ids, YOLO modes, or OS sandboxes.
- Do not copy full chat transcripts into `~/.agents/history/` or this repo.
