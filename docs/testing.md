# Ferumind Testing Guide

## Verification

Run the complete local quality gate before publishing:

```bash
just verify
```

The pipeline runs six steps in order: it rejects unsafe tracked public files
and movable GitHub Action references, holds the complexity ratchet, then checks
formatting, linting, strict typing, and tests with coverage. The equivalent
commands are:

```bash
uv run python scripts/check_public_tree.py
uv run python scripts/complexity_ratchet.py
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest --cov=src/ferumind
```

Coverage must remain at or above 80%.

## Focused tests

Use pytest directly while developing:

```bash
uv run pytest tests/unit/test_paths.py -x -v
uv run pytest tests/integration/test_mcp_surface.py -x -v
```

Filesystem tests use `tmp_path` and exercise success and adversarial failure
paths. Path containment, symlink escape, project scoping, proposal conflicts,
snapshot protection, and out-of-band reconciliation must be tested without
mocking the safety behavior under test.

MCP integration tests must treat every call as stateless, pass the required
`project` argument to project-scoped tools, verify strict input schemas and
structured envelopes, and cover the full propose → apply workflow.
