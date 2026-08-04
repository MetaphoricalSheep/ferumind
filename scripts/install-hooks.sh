#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing git hooks from .githooks/"
git config core.hooksPath .githooks

echo "==> Making hooks and scripts executable"
chmod +x "$ROOT/.githooks/pre-commit" "$ROOT/.githooks/pre-push"
chmod +x "$ROOT/scripts/"*.sh

echo "==> Done. Hooks are active."
