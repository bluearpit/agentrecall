# Contributing

The CLI is `agentrecall`. The PyPI package is `agentrecall-cli`. Source lives under `src/agentrecall`; tests under `tests`.

## Setup

```bash
uv sync --group dev
uv run agentrecall status
```

Python 3.11+. `uv` on `PATH`.

## Checks

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest
package_version="$(uv version --short)"
uv build --no-sources
uv run --isolated --no-project --with "dist/agentrecall_cli-${package_version}-py3-none-any.whl" tests/smoke_test.py
uv run --isolated --no-project --with "dist/agentrecall_cli-${package_version}.tar.gz" tests/smoke_test.py
```

CI runs the same lint, pytest on 3.11, 3.12, and 3.13, and distribution smoke tests. The `ci` job must be green before merge.

Tests always get a fake `HOME` (autouse fixture). Do not talk to the real home directory.

## Pull requests

`main` is protected: squash-merge a PR after `ci` passes. Direct pushes and force-pushes are blocked. No extra reviewer is required.

Keep the change small. Match the surrounding style (`AGENTS.md`): pathlib, explicit preconditions, write commands dry-run unless `--apply`.

Do not copy full chat transcripts into `~/.agents/history/` or this repo. Do not copy MCP ids, YOLO modes, or OS sandbox profiles into the permission translator.

## Releases

Update the version in `pyproject.toml`, then publish the matching GitHub release (`vX.Y.Z`). That release uploads `agentrecall-cli` to PyPI via Trusted Publishing. Manual Publish workflow runs build and smoke checks only; they never upload to PyPI.
