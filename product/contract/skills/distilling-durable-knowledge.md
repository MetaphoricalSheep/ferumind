---
name: distilling-durable-knowledge
description: Use when a phase or stage ends, a canvas has bloated, an incident reached an outcome, several episodes describe the same lesson, a source or research bundle needs integrating, the same fact keeps being re-derived, or the user asks to distill, promote, or document what was learned.
---

# Distilling durable knowledge

Distillation is how a workspace stops re-deriving what it already learned. The
architecture for it already exists; this is the method.

Work through the steps in order. Stopping early is a valid outcome — step 2 has
a "no material" answer, and forcing an artifact is worse than producing none.

## Where knowledge lives

| Layer | Holds |
|---|---|
| `memory/episodes/` | what happened |
| curated `memory/` | what future chats should remember |
| `library/` | settled, reusable knowledge |

The lifecycle runs: a real event → an episode preserves the experience →
curated memory preserves immediate continuity → later distillation → a
`library/` page captures the reusable lesson.

**Distillation never deletes or rewrites the episode.** History and distilled
knowledge are different assets. A library page distilled from three episodes
cites them and leaves all three exactly as they are.

## Step 1 — Search existing knowledge first

Before creating any durable document, look for one that already owns the
concept. Search `library/`, curated memory, relevant canvases, and episodes —
and search **synonyms and aliases**, because a "runbook" and a "playbook" for
the same thing will not match the same query.

`search_project` returns section-level hits with line ranges, so "does
something already cover this?" is usually answered by reading a section rather
than a whole document.

There is no backlink feature — nothing computes or exposes inbound links, so
this step is search plus judgement. Do not go looking for a backlink tool.

**Do not create a new durable document merely because new material arrived.**

## Step 2 — Classify the incoming material

- **New** — genuinely new durable knowledge.
- **Update** — materially improves an existing durable concept.
- **Disputed** — conflicts with existing evidence or knowledge.
- **No material** — nothing worth promoting.

These overlap freely, and **"no material" is a real outcome**. They are
procedure concepts only: never write them into frontmatter, and never add a
key, enum, or `status` value for them.

## Step 3 — Merge before proliferating

If an existing durable document owns the concept, update it. Create a new
document only for genuinely distinct knowledge.

Two failure modes, and they pull in opposite directions:

- **Proliferation** — a forest of thin one-source pages about the same
  concept, so the knowledge is never anywhere in full.
- **The catch-all** — one ever-growing file everything is appended to, so
  nothing in it is findable and the page becomes its own archive.

Aim for cumulative knowledge: fewer pages than sources, more pages than one.

## Step 4 — Ground important claims

Follow the source-grounding conventions in the workspace rules. Verify exact
numbers, dates, quotations, and precise externally sourced claims against the
evidence *before* writing them, and keep enough of a derivation that someone
can check it later.

## Step 5 — Handle disagreement honestly

Do not silently overwrite an old claim because a newer source disagrees.

Where the history matters, say in ordinary prose that the earlier
understanding was superseded and why, or record that the evidence disagrees.
Prose first — **do not invent a status ontology** for this. Episodes are
historical evidence rather than standing instructions, which is exactly how a
superseded claim should be read.

## Step 6 — Cascade review

After materially changing durable knowledge, search for the affected terms and
identify documents that may now be stale.

**Decide document by document. Do not mechanically rewrite anything.** This is
search-based for the same reason as step 1.

## Step 7 — Recommend a lint run

After substantial distillation, **recommend that the user run `ferumind
lint`** and say why: new documents and new source links are exactly what it
checks — broken links, duplicate ids, missing descriptions, and library pages
missing from a link web the project maintains.

You cannot run it yourself. It is a local operator command, not an MCP tool,
because its findings are only actionable with a human deciding. Recommend it;
do not attempt it.

## What this procedure never does

- Delete, rewrite, or version an episode.
- Add a frontmatter key, enum, or `status` value.
- Score knowledge for confidence, quality, or coverage.
- Rewrite links automatically, or claim backlinks exist.
- Run on a schedule, in the background, or without a human in the conversation.
