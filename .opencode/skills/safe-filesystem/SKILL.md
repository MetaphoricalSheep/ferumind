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

## Ways containment checks fail

The rules above say what to do. These are the specific wrong implementations,
each of which looks correct and passes an ordinary test:

- `str(path).startswith(str(root))` — admits the sibling `/tmp/root-evil` for
  the root `/tmp/root`.
- Normalizing by string replacement, which cannot see what the filesystem
  would resolve.
- Resolving the candidate but not the root, so a symlinked root compares
  against a path that no longer matches it.

Every containment check goes through `is_under_root()` in
`ferumind.core.paths`, and any change to path validation ships with
adversarial tests.

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

- Look up before editing: `search_project` → `read_document_range` on the
  hit's range → a granular `propose_*` tool → `apply_patch`. Call
  `get_document_map` only when broader structure is still needed (it can be
  large). Prefer `find_in_document` for an exact string in a known document.
  `read_document` is the expensive fallback.
- Line numbers alone are never sufficient. Every section/range/line/match edit
  is guarded by two hashes: the caller supplies `expected_document_sha256` plus
  a target hash (`expected_section_sha256` / `expected_range_sha256` /
  `expected_anchor_sha256`, or `expected_match_count`), and the proposal echoes
  back what it actually matched as `document_before_sha256` and
  `target_before_sha256`.
- Propose-time mismatches fail with `DOCUMENT_HASH_MISMATCH` /
  `TARGET_HASH_MISMATCH`; apply re-checks the document hash and fails with
  `PATCH_CONFLICT`. There is no best-effort overwrite.
- Frontmatter is protected: range/match edits overlapping the frontmatter block
  are rejected (`FRONTMATTER_PROTECTED`), required keys cannot be stripped
  (`FRONTMATTER_REQUIRED`), and `updated` is refreshed automatically.
- `.ferumind/` internals are never patchable through MCP.
- `propose_patch mode=body|full` is the coarse fallback for whole-document
  replacement only.
