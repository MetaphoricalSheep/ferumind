# Editing discipline

## Lookup first, patch narrow

Walk only as far as needed. Never `read_document` first:

1. `search_project` → section hits with `start_line`/`end_line`.
2. `read_document_range` on a hit (skip the map when the hit is enough).
3. `get_document_map` only for broader structure; it can be large.
4. `propose_exact_replace_patch` (preferred) or `propose_multi_edit_patch`
   for several edits to one document; positional patches
   (`propose_section_patch`, `propose_range_patch`, `propose_insert_patch`)
   for placement; `propose_frontmatter_patch` for metadata.
5. `apply_patch`.

`find_in_document` finds an exact string in a known document. Use
`propose_patch mode=body` only to replace a full body; `mode=full` only for
explicit full-file operations.

## A proposal is not a saved edit

Every `propose_*` result is pending until `apply_patch` returns
`document_mutated: true`. Never tell the user something is saved before
that. One targeted proposal, then apply — don't stack proposals. Withdraw
stale ones with `discard_patch`.

## Chain hashes, honor conflicts

After `apply_patch`, feed its returned `document_sha256` into the next
edit's `expected_document_sha256` instead of re-reading. If you get
`PATCH_CONFLICT`, the document changed under you (often the user editing by
hand — that is normal here): re-read, re-propose, never force.

## Honor the policy echo

Propose results echo the target's `edit_policy` and `status` with a
`policy_note`. The server will not stop you; honoring it is your job.
Verify-by-readback after edits to `propose-first` documents: quote the
changed lines back to the user.

## Creating documents

New documents go through `create_document` (or `capture_note` for inbox
items) — never through patches against paths that don't exist. Give every
new document a `description` (see the contract); when a document's purpose
drifts, correct it with `propose_frontmatter_patch` — it is not protected.
Give log canvases `edit_policy: append` and a month-stamped name
(`garden-log-2026-07.md`); start next month's file when the month turns.
