#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export LLM_MODE="${LLM_MODE:-offline}"
export DATABASE_URL="${DATABASE_URL:-sqlite:///$ROOT_DIR/data/sovereign.db}"

echo "=== [1/5] Ruff Lint ==="
uv run ruff check .

echo "=== [2/5] Mypy Type Check ==="
uv run mypy contracts dataset services evals tests

echo "=== [3/5] Seed Database ==="
uv run python -m dataset.seed

echo "=== [4/5] Pytest Suite ==="
uv run pytest -q

echo "=== [5/5] Golden Eval Suite (35 cases) ==="
uv run python -m evals.runner --models offline/planner-v1 offline/baseline-lite

echo "All Sovereign AI Stack checks passed."
