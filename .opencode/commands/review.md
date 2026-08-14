# Command: review

**Purpose:** Self-review before handing off changes. Diff review, search for red flags, and summarize risk.

## Steps

1. Review the diff:
   ```bash
   git diff
   ```
2. Search for red flags:
   - `TODO`, `FIXME`, `HACK`, `XXX`, `STUB`
   - `pass` as function body (outside stubs)
   - `Any` without justification comment
   - `# type: ignore` without justification comment
   - Broad `except Exception` without re-raise/logging
   - Global mutable state
   - Hardcoded absolute paths
   - Business logic in CLI/MCP layers
3. Check boundary violations — does any interface layer bypass core safety?
4. Check security implications — path operations, file writes, symlink handling.
5. Check tests cover the change adequately.
6. Summarize risk level (low/medium/high) and any concerns.

A red flag is not automatically a defect — `Any` and `# type: ignore` are
allowed with a written justification, and a `TODO` is allowed against a real
issue. What the review reports is the ones lacking that.
