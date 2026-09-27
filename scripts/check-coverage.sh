#!/usr/bin/env bash
set -euo pipefail

uv run --frozen pytest \
  --cov=agentrecall \
  --cov-branch \
  --cov-report=term-missing:skip-covered \
  --cov-fail-under=80
