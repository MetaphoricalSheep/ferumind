# Command: test-fix

**Purpose:** Make the current worktree green by running the full verification pipeline, fixing only safe verification failures, and escalating when the repair requires design or implementation judgment.

## Steps

1. Inspect the worktree first:
   ```bash
   git status --short
   git diff --stat
   ```
2. Run the preferred verification entrypoint:
   ```bash
   just verify
   ```
3. If verification fails, fix the first material failure with the smallest safe change.
4. Use targeted reruns only to speed up diagnosis:
   - Formatting only → `just format` or `uv run ruff format .`
   - Lint only → `just lint` or `uv run ruff check .`
   - Typecheck only → `just typecheck` or `uv run pyright`
   - Tests only → `just test`, `just test-cov`, or targeted `uv run pytest ...`
5. Re-run `just verify`.
6. Repeat until verification passes or escalation is required.

## Guardrails

- Do not implement missing feature work.
- Do not weaken tests, lower coverage, or hide failures.
- Do not change architecture, schema, dependencies, auth, path safety, snapshots, workspace isolation, or deletion behavior without escalation.

## Verification

- [ ] `just verify` passes
- [ ] Only safe verification fixes were applied
- [ ] No tests were weakened
