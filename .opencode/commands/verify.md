# Command: verify

**Purpose:** Run the full verification pipeline. If it fails, fix the underlying issue — do not bypass.

## Steps

1. Run the verification script:
   ```bash
   scripts/verify.sh
   ```
2. If any step fails, fix the root cause:
   - Format issues → `uv run ruff format .`
   - Lint errors → fix the code
   - Type errors → fix the types
   - Test failures → fix the code or tests
   - Coverage too low → add tests
3. Re-run `scripts/verify.sh` until it passes.

The script is the checklist: it exits non-zero on the first failing stage and
names it. Do not skip a stage or relax a threshold to get past one.
