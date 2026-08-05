---
name: mcp-hardening
description: Use when changing MCP tools, schemas, project scoping, proposal/apply behavior, transport boundaries, or structured error handling.
compatibility: opencode, claude-code, cursor, copilot, codex
metadata:
  project: ferumind
  role: mcp-security
---

# Skill: MCP Hardening

## Design Principles

- The server is stateless per call: no sessions, no `session_id`, no
  model-carried state except a short-lived patch `operation_id`
- Every project-scoped tool takes a required `project` argument — an
  assertion validated against the registry, never an override; a missing
  project returns `PROJECT_REQUIRED` and an unknown one `PROJECT_NOT_FOUND`
- Every response echoes the resolved project so wrong-project edits are not
  silent
- Tool input schemas must be strict
- No raw path writes — all paths resolve through core path validator
- All writes become snapshot-protected
- Tool responses must be structured (machine-readable)
- Errors must be machine-readable (typed error codes)
- No silent partial success — either fully succeed or report failure
- No hard delete of user content (`archive_document` / `unarchive_document`)
- Tool names should be stable snake_case, never dotted

## Security

- Every tool validates paths through `core.paths.contained_path()`
- No tool writes outside the project scope
- Operation log for every mutation
- Observation log records metadata only, with secret redaction

## Lookup-First Granular Editing

- Provide lookup tools so an assistant can target the smallest safe edit:
  `get_document_map`, `read_document_range`, `find_in_document` (read-only:
  `readOnlyHint: true, idempotentHint: true`).
- Provide granular propose tools — `propose_section_patch`,
  `propose_range_patch`, `propose_search_replace_patch`, `propose_insert_patch` —
  in addition to the coarse `propose_patch` fallback. Propose tools write
  operation records, so they are **not** read-only.
- Every granular edit is guarded by two hashes: `expected_document_sha256` plus a
  target hash (`expected_section_sha256` / `expected_range_sha256` /
  `expected_anchor_sha256`, or `expected_match_count` for search/replace).
- Proposals are bound to project + path + base document hash and expire
  (`PATCH_EXPIRED`); out-of-band disk edits invalidate them
  (`PATCH_CONFLICT`, fail closed — never clobber).
- Propose results echo the target's `edit_policy`/`status` with a
  `policy_note`; the server informs, agents honor. Hard refusals are the
  closed list: `DOCUMENT_ARCHIVED`, `FRONTMATTER_PROTECTED`,
  `WORKSPACE_MISMATCH`.
- Errors are machine-readable codes returned through `FerumindToolEnvelope`:
  `SECTION_NOT_FOUND`, `RANGE_NOT_FOUND`, `RANGE_TOO_LARGE`, `MATCH_NOT_FOUND`,
  `AMBIGUOUS_MATCH`, `INVALID_REGEX`, `FRONTMATTER_PROTECTED`,
  `FRONTMATTER_REQUIRED`, `PATCH_CONFLICT`, `DOCUMENT_HASH_MISMATCH`,
  `TARGET_HASH_MISMATCH`, `VALIDATION_ERROR`, `PROJECT_REQUIRED`,
  `PROJECT_NOT_FOUND`, `PATCH_EXPIRED`, `DOCUMENT_ARCHIVED`,
  `UNKNOWN_FOLDER`, `CANNOT_ARCHIVE_SPINE`, `PATH_EXISTS`.
- Tool names stay snake_case (never dotted); every tool exposes `outputSchema`
  and returns `FerumindToolResult`.
