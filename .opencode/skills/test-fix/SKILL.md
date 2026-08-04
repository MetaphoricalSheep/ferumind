---
name: test-fix
description: Use during plan verification, post-implementation cleanup, CI repair, or completion checks to run Lattice verification, fix safe test/lint/type failures, and escalate unsafe failures.
compatibility: opencode, claude-code, cursor, copilot, codex
metadata:
  project: lattice
  role: verification-repair
  preferred-command: just verify
  canonical-command: scripts/verify.sh
---

# Test Fix Skill

Use this skill whenever the current worktree must be made green.

This includes:

* a user explicitly asking to fix tests, lint, typecheck, or CI
* a planning agent reaching the verification phase of an implementation plan
* a primary coding agent finishing implementation work
* a branch needing final validation before completion
* a failed CI or local verification run needing diagnosis

This is not a feature implementation skill.

## Mission

Run the Lattice verification suite, diagnose failures, apply the smallest safe fixes, and escalate when the fix requires architecture, product behavior, security, schema, dependency, or implementation judgment.

## Preferred verification command

just verify

This is the preferred entrypoint because it matches the repo's documented developer workflow.

## Canonical verification command

scripts/verify.sh

A task is not complete until the full verification pipeline exits successfully.

## Required loop

1. Inspect the worktree with git status and git diff.
2. Run `just verify`.
3. If verification fails, identify the first material failure.
4. Apply the smallest correct safe fix.
5. Use targeted reruns only to shorten diagnosis.
6. Rerun `just verify` before declaring success.
7. Repeat until verification passes or escalation is required.

## Safe fixes

You may fix:

* formatting failures
* Ruff lint failures
* Pyright type failures
* broken imports
* missing narrow type annotations
* deterministic fixture or expectation drift caused by already-applied changes
* small bugs directly proven by failing tests
* focused tests needed to satisfy coverage, when the behavior is already implemented and clear

## Forbidden fixes

Do not:

* implement new features
* redesign architecture
* change product behavior without an approved plan
* change public APIs without an approved plan
* alter database schema or migrations
* modify auth, path safety, deletion semantics, snapshots, workspace isolation, operation logs, or MCP safety without escalation
* weaken, delete, skip, xfail, or fake tests
* lower coverage thresholds
* add broad mocks to hide real failures
* silence lint or type errors instead of fixing the cause
* add Any or type-ignore comments without written justification and escalation
* commit, push, reset, clean, or destructively remove files

## Lattice constraints

Follow AGENTS.md.

Important constraints:

* Core logic belongs in lattice.core.
* CLI, MCP, workers, and agents must call core logic instead of duplicating it.
* Use pathlib, not os.path.
* Public functions need explicit parameter and return types.
* Path-security code requires adversarial tests.
* Never write live user workspace data to git.
* Never use string-prefix path containment checks.
* Prefer existing is_under_root and contained_path patterns.

## Escalation conditions

Stop and escalate if any condition is met:

1. `just verify` still fails after 3 repair cycles.
2. The fix requires architecture or product behavior decisions.
3. The failure touches path safety, symlink escape, snapshots, operation logs, workspace isolation, MCP tool safety, auth, or deletion semantics.
4. The apparent fix requires weakening tests or lowering quality gates.
5. The failure appears caused by a wrong or incomplete implementation plan.
6. Dependencies or environment/config contracts need to change.
7. You are unsure whether code behavior or test behavior is correct.
8. The fix would require implementing missing feature work rather than repairing verification failures.

## Success response

## TEST FIX COMPLETE

### Final verification

`just verify` passed.

### Files changed

* ...

### Fix summary

...

### Risks / notes

...

## Escalation response

## ESCALATION REQUIRED

### Failing command

...

### Failure summary

...

### What I tried

1. ...
2. ...
3. ...

### Files changed

* ...

### Why I stopped

...

### Recommended escalation

* planner / architect / implementation agent / human decision
