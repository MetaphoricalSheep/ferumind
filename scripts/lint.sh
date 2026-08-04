#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> ruff check"
uv run ruff check .

echo "==> ruff format check"
uv run ruff format --check .
