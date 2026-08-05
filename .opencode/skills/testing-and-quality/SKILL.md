---
name: testing-and-quality
description: Use when planning tests, repairing coverage, reviewing verification gates, or adding deterministic unit, integration, and adversarial checks.
compatibility: opencode, claude-code, cursor, copilot, codex
metadata:
  project: ferumind
  role: quality-engineering
---

# Skill: Testing and Quality

## Test Pyramid

1. **Unit tests** — pure core logic, no I/O
2. **Integration tests** — CLI/MCP boundaries
3. **Adversarial tests** — path escape, symlink attacks
4. **Snapshot/rollback tests** — mutation recovery and restore behavior

## Coverage

- Global minimum: 80% (fails build if below)
- Target during implementation: 90%+
- No lowering thresholds without explicit user instruction

## Rules

- Use `tmp_path` for filesystem tests
- No mocking core behavior unless necessary
- Tests must be deterministic
- No tests that simply assert imports
- Test both success and failure paths
- Path-security code requires adversarial tests
- Regression tests for every bug
