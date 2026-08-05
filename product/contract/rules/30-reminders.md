# Reminders and triggers

A project that needs the agent to bring something up later keeps one ledger:
`memory/reminders-and-triggers.md`. Create it the first time there is
something to remember; don't seed an empty one.

## It is a ledger, not a notes page

An entry belongs here only if you are expected to actively surface it later.
Standing rules, metrics, protocols, plans, and background context live in the
spine, `rules/`, canvases, or other memory files — not here. If an item would
never make you say something unprompted, it is not a reminder.

## Every entry answers four questions

A stable kebab-case **id**, **when it fires**, **what to raise with the
user**, and **when it goes quiet or closes**. Three sections:

- **Repeatable** — recurring work. These never leave the ledger. They fire
  when the current period's entry is missing and stay quiet once it exists,
  so each one needs a suppression condition, not just a cadence.
- **Open** — fires once. Threshold, date, or event.
- **Closed** — fired-and-finished, with the date and what actually happened.

A table with a column per question is the usual shape:

```markdown
| ID | Cadence | Fire when | Remind the user to | Suppress when |
| ID | Status | Trigger | Remind the user to | Close when |
| ID | Closed | Outcome |
```

## Firing discipline

Read the ledger at the start of a conversation and check it again when the
work turns toward something it covers. Surface only entries whose trigger is
genuinely met, together and once — not drip-fed through the conversation, and
not a recital of everything on the list. If you cannot tell whether the thing
was already done, ask once; do not nag, and do not prompt for a routine "no
change" answer.

## Close, never delete

Move finished entries to Closed with a date and a real outcome — done,
skipped, deliberately postponed, or superseded by a newer entry. The trail of
what fired and what came of it is the point. Trim the Closed list by
archiving the file and starting a fresh one, not by erasing rows.

## Adapt the shape

This is a floor, not a ceiling. Add columns, split by category, group by
phase, or invent a section the project needs — as long as an entry still says
when it fires, what to raise, and when it stops.
