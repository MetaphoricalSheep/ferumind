#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "══════════════════════════════════════════════════"
echo "  Ferumind — Full Verification"
echo "══════════════════════════════════════════════════"

echo ""
echo "==> [1/5] public-tree and action-pin checks"
uv run python scripts/check_public_tree.py || { echo "FAILED: public-tree checks"; exit 1; }

echo ""
echo "==> [2/5] ruff format check"
uv run ruff format --check . || { echo "FAILED: ruff format"; exit 1; }

echo ""
echo "==> [3/5] ruff lint"
uv run ruff check . || { echo "FAILED: ruff check"; exit 1; }

echo ""
echo "==> [4/5] pyright type check"
uv run pyright || { echo "FAILED: pyright"; exit 1; }

echo ""
echo "==> [5/5] pytest with coverage"
uv run pytest --cov=src/ferumind --cov-report=term-missing --cov-report=html --tb=short || { echo "FAILED: pytest"; exit 1; }

echo ""
echo "══════════════════════════════════════════════════"
echo "  All checks passed."
echo "══════════════════════════════════════════════════"
