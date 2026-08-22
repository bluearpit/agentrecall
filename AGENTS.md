# agentrecall

Python 3.11+ CLI. Source lives under `src/agentrecall`. Tests live under `tests` and must use a fake `HOME` (the `home` fixture is autouse).

The PyPI package is `agentrecall-cli`; the import and command stay `agentrecall`.

- Use pathlib and explicit preconditions.
- Write commands default to dry-run; `--apply` writes.
- Translate only the small `permissions.yaml` policy; do not copy MCP ids, YOLO modes, or OS sandbox profiles. `external_write` becomes extra writable roots where a tool documents them.
- Do not copy full chat transcripts into `~/.agents/history/` or this repo.
- CI: `ruff check`, `ruff format --check`, pytest on 3.11–3.13, and wheel/sdist smoke tests. The `ci` job is the merge gate.
- Human setup and PR rules: `CONTRIBUTING.md`.
