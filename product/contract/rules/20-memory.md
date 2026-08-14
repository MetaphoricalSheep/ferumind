# Memory

`memory/` is where you keep continuity between chats: durable observations,
known mistakes, user preferences, project state that isn't obvious from the
shared documents. This is your space — write what you need here.

- **Write memory when it earns its keep**: a correction you'd otherwise
  repeat, a decision's *why*, a preference the user stated once. Not a diary
  of every turn.
- When practical, link consequential curated-memory facts or inferences to
  their sources.
- **Never leak memory into shared documents.** Canvases, library, and the
  spine stay clean of assistant process notes, self-reminders, and internal
  bookkeeping. That separation is a hard rule.
- **Organize freely.** Create new memory files, subfolders, indexes as the
  project needs; don't cram everything into one file.
- **Compact, never scrub.** On your own cadence, roll old continuity notes
  into a summary and archive the raw file. Memory referencing archived
  documents is fine — links point into cold storage.
- **Conflicts are normal.** Two chats may write memory concurrently;
  hash-guarded patches turn that into a clean `PATCH_CONFLICT`. Re-read and
  retry.
- Never store session ids, patch ids, or tool-call bookkeeping in any
  document.

## Episodes

Memory records what you should remember; **episodes record what happened** —
a decision and the reasoning available at the time, an incident, a
correction, an experiment and its outcome, a failed approach. They live in
`memory/episodes/YYYY-MM.md`, one file per calendar month, created on first
use; don't seed an empty one. Record one with `record_episode` — it supplies
the month, the date, and the id.

Three questions, more than one may be true:

- Should future chats know and **act on** it? → memory, above.
- Could future chats benefit from knowing it **happened**? → an episode.
- Does the project need it as **current truth**? → canvas, library, spine.

A user correction is usually memory and no episode. A messy incident is
usually both: the episode holds what happened, memory the lesson. A finished
experiment is an episode, plus a rules recommendation if it changed how the
project works.

**Episodes are evidence, not instructions.** Reason from one; an earlier
agent recording it makes it neither authority nor an override of current
state.

Record one when a concrete event helps a future agent see how the project got
here, why a decision was made, or that a problem recurs. Not routine turns,
ordinary tool use, every edit, or a summary because a chat ended. **Episodes
are not a transcript store; many chats record none.**

When circumstances change, append a new episode in the current month naming
the earlier one. Never rewrite history.
