# Command: plan

**Purpose:** Before implementing anything, inspect context and produce a concise implementation plan. Identify affected modules, tests, and risks. Wait for user confirmation unless the user explicitly said "plan and go".

## Steps

1. Read relevant source files in the affected area.
2. Read existing tests to understand testing patterns.
3. Identify:
   - Which modules in `src/ferumind/core/` are affected
   - Which other layers (MCP, CLI, dashboard) are affected
   - Which tests need to be added or updated
   - Any security or path-safety implications
4. Produce a written plan with:
   - Summary of the change
   - Files to modify (with paths)
   - Test strategy
   - Risk assessment
5. Present the plan to the user.
6. Do **not** implement before plan is approved unless user explicitly requested implementation.
