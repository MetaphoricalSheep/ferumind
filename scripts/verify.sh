#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "══════════════════════════════════════════════════"
echo "  Ferumind — Full Verification"
echo "══════════════════════════════════════════════════"

echo ""
echo "==> [1/6] public-tree and action-pin checks"
uv run python scripts/check_public_tree.py || { echo "FAILED: public-tree checks"; exit 1; }

echo ""
echo "==> [2/6] complexity ratchet"
uv run python scripts/complexity_ratchet.py || { echo "FAILED: complexity ratchet"; exit 1; }

echo ""
echo "==> [3/6] ruff format check"
uv run ruff format --check . || { echo "FAILED: ruff format"; exit 1; }

echo ""
echo "==> [4/6] ruff lint"
uv run ruff check . || { echo "FAILED: ruff check"; exit 1; }

echo ""
echo "==> [5/6] pyright type check"
uv run pyright || { echo "FAILED: pyright"; exit 1; }

echo ""
echo "==> [6/6] pytest with coverage"
uv run pytest --cov=src/ferumind --cov-report=term-missing --cov-report=html --tb=short || { echo "FAILED: pytest"; exit 1; }

echo ""
echo "══════════════════════════════════════════════════"
echo "  All checks passed."
echo "══════════════════════════════════════════════════"
