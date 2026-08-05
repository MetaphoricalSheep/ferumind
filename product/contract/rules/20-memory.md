# Memory

`memory/` is where you keep continuity between chats: durable observations,
known mistakes, user preferences, project state that isn't obvious from the
shared documents. This is your space. You are free to utilize it and write
whatever you want here.

- **Write memory when it earns its keep**: a correction you'd otherwise
  repeat, a decision's *why*, a preference the user stated once. Not a diary
  of every turn.
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
