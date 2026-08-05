---
name: safe-filesystem
description: Use when changing filesystem paths, reads, writes, moves, archives, snapshots, permissions, locking, or workspace boundaries.
compatibility: opencode, claude-code, cursor, copilot, codex
metadata:
  project: ferumind
  role: filesystem-security
---

# Skill: Safe Filesystem Operations

## Core Rules

- No symlink escape — `assert_no_symlink_escape()` before any file operation
- No `..` traversal — all user paths through `contained_path()`
- No absolute path writes
- No writing through user-controlled paths without canonicalization
- Allowed extensions checked before write
- Atomic writes (write to temp, rename)
- File/project locks before mutation
- Snapshot before every mutation
- Operation log for every mutation
- Restore path for every mutation

## Forbidden path-security patterns

- Never use string prefix checks such as `str(path).startswith(str(root))` for containment.
- Never use naive string replacement to normalize paths.
- Never trust user-supplied paths.
- Never join user input directly to workspace/project roots without validation.
- Never resolve only the candidate path while leaving the root unresolved.
- Never allow absolute paths where the API expects project-relative paths.
- Never follow symlinks without validating the final resolved path remains inside the allowed root.
- All containment checks must go through the central `is_under_root()` helper in `ferumind.core.paths`.
- Any change to path validation requires adversarial tests.

## Safety Flow

1. Canonicalize the user-provided path
2. Verify it's under the allowed root through `is_under_root()`
3. Check for symlink escape
4. Check allowed extension
5. Acquire lock
6. Snapshot existing content
7. Perform write (atomic)
8. Log operation
9. Release lock

## Granular Edits Are Lookup-First and Hash-Guarded

Prefer the narrowest safe edit target over replacing a whole Markdown body.

- Look up before editing: `get_document_map` → `find_in_document` /
  `read_document_range` → a granular `propose_*` tool → `apply_patch`.
- Line numbers alone are never sufficient. Every section/range/line/match edit
  must carry both a `document_before_sha256` and a `target_before_sha256`.
- Propose-time mismatches fail with `DOCUMENT_HASH_MISMATCH` /
  `TARGET_HASH_MISMATCH`; apply re-checks the document hash and fails with
  `PATCH_CONFLICT`. There is no best-effort overwrite.
- Frontmatter is protected: range/match edits overlapping the frontmatter block
  are rejected (`FRONTMATTER_PROTECTED`), required keys cannot be stripped
  (`FRONTMATTER_REQUIRED`), and `updated` is refreshed automatically.
- `.ferumind/` internals are never patchable through MCP.
- `propose_patch mode=body|full` is the coarse fallback for whole-document
  replacement only.
