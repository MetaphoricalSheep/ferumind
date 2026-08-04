# Command: security-review

**Purpose:** Review security-sensitive changes before handoff.

## When to run

Before completing any task that touches path handling, file writes, project scoping, snapshots, MCP tools, config loading, or workspace access.

## Security review checklist

- Did this change touch path handling, file writes, project scoping, snapshots, MCP tools, config loading, or workspace access?
- Are all filesystem operations routed through `lattice.core` safety helpers?
- Are all user-provided paths treated as untrusted?
- Are absolute paths rejected where relative paths are expected?
- Are `..` traversal attempts rejected?
- Are symlink escapes rejected after resolution?
- Are sibling-prefix escapes rejected? Example: `/tmp/root` vs `/tmp/root-evil`.
- Are writes project-scoped?
- Is there any hard delete? If yes, stop and redesign around archive/snapshot.
- Are errors machine-readable where relevant?
- Are there tests for both success and failure paths?
- Did full verification run?

## Verification

```bash
scripts/verify.sh
```

For targeted path-security changes:

```bash
uv run pytest tests/unit/test_paths.py tests/unit/test_security.py tests/unit/test_project_structure.py
uv run pyright
uv run ruff check .
```
