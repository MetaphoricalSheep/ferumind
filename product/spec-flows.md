# Spec: Flows

Status: locked for build · 11 Jul 2026 · the end-to-end behaviors the specs
must add up to. Actors: **U** (user), **A** (chat agent), **S** (Ferumind MCP
server), **W** (mechanical worker).

## 1. Chat startup

1. U opens a chat in a client project whose system prompt is the bootstrap
   (project key pinned).
2. A calls `get_context(project)` → rules + spine + map + inbox count. One
   call, no session, nothing to recover.
3. A handles U's message under those rules.

Unreachable server → A says so plainly before advising from memory
(bootstrap rule). No other failure handling is A's job.

## 2. An edit

```
search_project → get_document_map → find_in_document / read_document_range
→ propose_* (one, targeted) → apply_patch → chain returned document_sha256
```

- Propose result echoes `edit_policy`/`status` + `policy_note`; A honors it
  (append → additions only; propose-first → announce before apply;
  ask-human → only on explicit request this conversation).
- `PATCH_CONFLICT` on apply → re-read, re-propose. Often means U edited on
  disk; that's normal, not an error to escalate.
- Stale/superseded proposals → `discard_patch`.

## 3. Logging an entry ("bench 3x8 at 20 kg")

1. Rules/spine direct A to the current log canvas
   (`canvases/garden-log-2026-07.md`, `edit_policy: append`).
2. A appends via `propose_insert_patch` → `apply_patch`. If the month
   turned, A first creates the new month's file (`create_document`,
   `edit_policy: append`) per the editing rules.
3. Project rules say what follows unprompted (update progression, prep next
   session); A does it in the same turn and reports what changed.

*(v1: this behavior rides in always-loaded project rules. Cadence/due-ness
machinery is phase 2 — skills.)*

## 4. Rules change (always human-gated)

- U-initiated: "add a rule: X" → A proposes to the rules file, states the
  diff intent, applies (the request is the authorization), verifies by
  readback.
- A-recommended: A notices a repeated correction → "want me to add this to
  the rules?" → edits only on yes.
- Every such edit: snapshot + oplog.

## 5. Capture and triage

1. Stray thought in any chat → `capture_note(project, text)` → `inbox/`.
2. Later, in a normal chat ("let's clear the inbox", or A offers when
   `inbox_count > 0`): each item is filed (create/patch into `canvases/` or
   `library/`) or archived. Inbox trends toward empty.

## 6. Distillation and archive

Trigger: U says so ("phase 1 is done", "promote this to a runbook"), or A
suggests it at a natural moment (finished phase, bloated canvas, same fact
re-derived twice).

1. A confirms with U.
2. Distill: outcomes → successor canvas; durable insights → `memory/`;
   reference-grade material → `library/`.
3. `archive_document` on the retired doc. Log canvases are untouched — they
   roll by calendar, not by phase.
4. Spine map updated (`propose-first`: announce, apply, read back).

## 7. New project

1. `create_project(key, title)` seeds spine + folders from templates.
2. Copy the bootstrap prompt, fill in the project key, create the client-side
   project, and paste it.
3. First chat calls `get_context` and is operational. Nothing else to set
   up.

## 8. Hand-edit on disk (vim/Obsidian)

1. U edits any file directly. No ceremony required.
2. W (watcher) notices within the debounce window: snapshot + reindex +
   oplog `source: out-of-band`.
3. If the watcher missed it (server down, synced mount), the next agent
   read reconciles: reindex + oplog + pending proposals against the old
   hash marked stale.
4. A conflicting `apply_patch` fails closed.

The user never has to announce a hand-edit, and an agent never acts on a
stale copy at the point of use.
