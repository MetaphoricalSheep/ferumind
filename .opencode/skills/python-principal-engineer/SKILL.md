---
name: python-principal-engineer
description: Use when designing, implementing, or reviewing typed Python modules, public APIs, architecture boundaries, or dependency choices.
compatibility: opencode, claude-code, cursor, copilot, codex
metadata:
  project: ferumind
  role: python-engineering
---

# Skill: Python Principal Engineer

## Standards

- Modern Python 3.12+ with full typing
- Small cohesive modules — no god files, no circular imports
- Explicit module boundaries with clean `__all__` exports
- Dependency inversion where useful — core has no framework leakage
- Pydantic v2 models for config/schema validation at system boundaries
- Dataclasses or value objects for simple internal structures
- Clean error types — no broad `except Exception` without re-raise/logging and tests
- No hidden global state for app/session behavior
- No premature abstractions, but no scripts-as-architecture either
- Every public function has explicit parameter and return types

## Forbidden

- `Any` without written justification
- `# type: ignore` without written justification
- Broad unstructured dicts at module boundaries where a typed model is appropriate
- Hardcoded absolute paths
- Side effects at import time
