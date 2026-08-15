# Spec: Flows

Status: locked for build · 11 Jul 2026 · the end-to-end behaviors the specs
must add up to. Actors: **U** (user), **A** (chat agent), **S** (Ferumind MCP
server). There is no worker actor — mechanical work runs inline on the calling
path (00 D13).

## 1. Chat startup

1. U opens a chat in a client project whose system prompt is the bootstrap
   (project key pinned).
2. A calls `get_context(project)` → rules + spine + map + skill index + inbox
   count. One call, no session, nothing to recover.
3. A handles U's message under those rules.

Unreachable server → A says so plainly before advising from memory
(bootstrap rule). No other failure handling is A's job.

## 2. An edit

```
search_project → read_document_range (on the hit's start_line/end_line)
→ propose_* (one, targeted) → apply_patch → chain returned document_sha256
```

`get_document_map` only when broader structure is still needed (it can be
large). `find_in_document` for an exact string in a known document.
`read_document` is the expensive fallback — not the first move.
- Propose result echoes `edit_policy`/`status` + `policy_note`; A honors it
  (append → additions only; propose-first → announce before apply;
  ask-human → only on explicit request this conversation).
- `PATCH_CONFLICT` on apply → re-read, re-propose. Often means U edited on
  disk; that's normal, not an error to escalate.
- Stale/superseded proposals → `discard_patch`.

## 3. Logging an entry ("two rows of garlic in bed three")

1. Rules/spine direct A to the current log canvas
   (`canvases/garden-log-2026-07.md`, `edit_policy: append`).
2. A appends via `propose_insert_patch` → `apply_patch`. If the month
   turned, A first creates the new month's file (`create_document`,
   `edit_policy: append`) per the editing rules.
3. Project rules say what follows unprompted (update progression, prep next
   session); A does it in the same turn and reports what changed.

*(This behavior rides in always-loaded project rules. Skills carry the
on-demand procedures instead, matched by situational trigger; cadence and
due-ness remain deliberately unbuilt.)*

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

1. A confirms with U. The `distilling-durable-knowledge` skill is the
   procedure; `get_context` carries its trigger and `read_skill` its body.
2. Distill: outcomes → successor canvas; durable insights → `memory/`;
   reference-grade material → `library/`. New or confidently updated
   evidence-derived library claims keep a `## Sources` list; provenance is
   never fabricated to backfill history.
3. `archive_document` on the retired doc. Log canvases are untouched — they
   roll by calendar, not by phase.
4. Spine map updated (`propose-first`: announce, apply, read back).

## 7. Recording an episode

Trigger: something happened that a future chat may want to know *happened* —
a decision and its reasoning, an incident, a correction, an experiment's
outcome. Not every turn; most chats record none.

1. A classifies: act on it later → `memory/`; happened → an episode; current
   shared truth → canvas, library, or spine. More than one may apply.
2. A appends the episode to `memory/episodes/YYYY-MM.md` (created on first
   use, never seeded empty).
3. Circumstances change later → a **new** episode in the current month naming
   the earlier one. The old record is never rewritten.
4. Retrieval is the ordinary surface: `search_project(folder="memory")`,
   `get_document_map`, `find_in_document`. Episodes feed §6 distillation as
   raw material; distilling one does not edit it.

Episodes are evidence, not standing instructions. Precedence is unchanged:
rules are the human's standing instructions and the spine still wins.

## 8. New project

1. `create_project(key, title)` seeds spine + folders from templates.
2. Copy the bootstrap prompt, fill in the project key, create the client-side
   project, and paste it.
3. First chat calls `get_context` and is operational. Nothing else to set
   up.

## 9. Hand-edit on disk (vim/Obsidian)

1. U edits any file directly. No ceremony required.
2. The next agent read that touches the path reconciles: reindex + oplog
   `source: out-of-band` + pending proposals against the old hash marked
   stale.
3. A conflicting `apply_patch` fails closed.

There is no watcher (removed 2026-08-06; see 00 D12). Detection happens at
read time, which is the point where staleness could actually cause harm.

The user never has to announce a hand-edit, and an agent never acts on a
stale copy at the point of use.
