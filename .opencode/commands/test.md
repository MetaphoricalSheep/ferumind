# Command: test

**Purpose:** Testing workflow — run relevant tests, add missing tests, run full coverage.

## Steps

1. Run the most specific tests first:
   ```bash
   uv run pytest tests/unit/test_PATH.py -x -v
   ```
2. If tests are missing for new behavior, add them.
3. Run full test suite with coverage:
   ```bash
   uv run pytest --cov=src/lattice --cov-report=term-missing
   ```
4. Do **not** weaken tests to make them pass.
5. Do **not** lower coverage thresholds without explicit user instruction.
6. If coverage fails, add tests to cover the gap.

## Verification

- [ ] All tests pass
- [ ] Coverage >= 80%
- [ ] No tests weakened
- [ ] Missing tests for new behavior added
