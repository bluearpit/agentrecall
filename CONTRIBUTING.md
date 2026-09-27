# Contributing

The CLI is `agentrecall`. The PyPI package is `agentrecall-cli`. Source lives under `src/agentrecall`; tests under `tests`.

## Setup

```bash
uv sync --group dev
uv run pre-commit install
uv run agentrecall status
```

Python 3.11+. `uv` on `PATH`.

## Checks

```bash
uv run ruff check src tests
uv run ruff format --check src tests
bash scripts/check-coverage.sh
uv run pre-commit run --all-files
package_version="$(uv version --short)"
uv build --no-sources
uv run --isolated --no-project --with "dist/agentrecall_cli-${package_version}-py3-none-any.whl" tests/smoke_test.py
uv run --isolated --no-project --with "dist/agentrecall_cli-${package_version}.tar.gz" tests/smoke_test.py
```

The pre-commit hook runs Ruff and the full pytest suite with branch coverage of at least 80%. CI runs the same coverage gate on 3.11, 3.12, and 3.13, plus lint and distribution smoke tests. The `ci` job must be green before merge, even if a local hook is skipped. Coverage measures executed code; it cannot prove a specific behavior is asserted. Add a regression test for every bug fix that fails before the fix and passes after it.

Tests always get a fake `HOME` (autouse fixture). Do not talk to the real home directory.

## History modules

`history.py` is the public query and indexing entry point used by the CLI. The modules below it have one responsibility each:

- `history_models.py` defines session, command, search-hit, and turn records.
- `history_sources.py` discovers native files and associates sessions with projects.
- `history_parsing.py` reads Claude, Cursor, Codex, and Pi JSONL and normalizes their content. It does not access SQLite.
- `history_store.py` owns the SQLite schema, WAL and retry behavior, writes, and query SQL. It does not parse transcripts.

When adding an agent, put its file discovery in `history_sources.py` and its format handling in `history_parsing.py`. Keep the CLI using `history.py` so indexing and queries share the same path.

## Pull requests

`main` is protected: squash-merge a PR after `ci` passes. Direct pushes and force-pushes are blocked. No extra reviewer is required.

Keep the change small. Match the surrounding style (`AGENTS.md`): pathlib, explicit preconditions, write commands dry-run unless `--apply`.

Do not copy full chat transcripts into `~/.agents/history/` or this repo. Do not copy MCP ids, YOLO modes, or OS sandbox profiles into the permission translator.

## Releases

Update the version in `pyproject.toml`, then publish the matching GitHub release (`vX.Y.Z`). That release uploads `agentrecall-cli` to PyPI via Trusted Publishing. Manual Publish workflow runs build and smoke checks only; they never upload to PyPI.
