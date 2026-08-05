# Command: implement

**Purpose:** Implement an approved plan. Keep changes focused, maintain module boundaries, add/update tests, and run verification.

## Steps

1. Make focused changes to the identified files.
2. Maintain core/interface boundaries — no business logic in CLI/MCP.
3. Add or update tests for every new behavior.
4. Run targeted checks on changed modules:
   ```bash
   uv run ruff format --check src/ferumind/core/paths.py
   uv run ruff check src/ferumind/core/paths.py
   uv run pyright src/ferumind/core/paths.py
   uv run pytest tests/unit/test_paths.py -x
   ```
5. For broad changes, run full verification:
   ```bash
   scripts/verify.sh
   ```

## Verification

- [ ] Changes are focused on the approved scope
- [ ] No boundary violations (core vs interface)
- [ ] Tests added/updated for all new behavior
- [ ] Targeted checks pass
- [ ] Full verify passes for broad changes
