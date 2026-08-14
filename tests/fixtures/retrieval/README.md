# The retrieval benchmark corpus

Synthetic fixtures and gold labels for the retrieval harness. Everything here is
invented. See [docs/retrieval-harness.md](../../../docs/retrieval-harness.md)
for what the numbers mean.

## The one rule that is absolute

**No real workspace content, ever.** Not a project, not a document, not an
excerpt, not a query. Where a real case inspires a fixture, the fixture is
**rewritten from scratch**, never anonymised — anonymisation of prose is
unreliable and there is no way to verify it after the fact.

Queries are content too. A real query set discloses what a workspace is about
as surely as its documents do.

Nothing shaped like a credential, token, key or hostname belongs here either,
including plausible fakes: they trip secret scanning and waste a human's time.
`.invalid` is the only permitted domain (RFC 2606 reserves it, so it can never
resolve). `test_corpus_integrity.py` enforces all of this.

## What the corpus is

A fictional volunteer weather-station network in a fictional valley. **38**
fixture documents across `canvases/`, `library/`, `memory/`, `rules/` and
`archive/`, plus the `spine.md` and `rules/00-project.md` that
`create_project` seeds — those two are indexed as distractors, which is what
they are in a real project. **40** documents are indexed in total.

RET-06 expanded the fixture set with vocabulary-overlapping distractors so
top-10 scoring cannot succeed merely by covering most of a tiny corpus. The
labelled query count stays at 70 unless an audit identifies a real coverage
hole.

Structural properties the tests depend on:

- **archived** documents (`archive/`, `status: archived`) — including the
  superseded upload schedule and an abandoned enclosure-vent draft;
- documents in **every role folder**, so RET-03's `folder` filter has fixtures;
- **non-`active` statuses** (`gated`, `frozen`), so its `status` filter does too.

## Adding a case

### 1. Write the intent, not the query

One line describing what someone would want to know. No document exists yet and
that is the point.

### 2. Get the query written blind

The query must be written by someone — or something — that has **not seen the
document it targets**. The corpus was built by three independent agents on
three different models writing candidate queries from intents alone, while
different agents wrote the documents from the same intents without ever seeing
the queries.

This matters more than it sounds. If the same author writes a document and then
"a paraphrase of it", they unconsciously reuse the words they just typed, the
case becomes an easy lexical match, and the baseline is flattered forever. You
cannot detect that afterwards by reading it.

### 3. Write the document from the intent

Again from the intent alone, not from the query. Prose, not bullet soup — a
list of keywords is trivially retrievable and measures nothing. Do not restate
the heading in the first sentence of a section; that is the single most common
way a fixture becomes an accidentally easy case.

### 4. Label at full resolution

`(path, section_id, evidence)`. The `section_id` is whatever
`derive_sections` produces for that heading; the evidence is text that occurs
**verbatim inside that section**. Both are checked, so a label that drifts
fails loudly rather than quietly making a query unanswerable.

Label sections **now**, even though the scorer can only score documents today.
Section retrieval is the next ticket, and relabelling a hundred queries by hand
at the moment you need an impartial referee is the trap this avoids.

### 5. Both phrasings

Every need carries two cases: `natural` (as a person would ask) and `keyword`
(the terse fragment). They target the same gold answer, so the gap between them
measures what phrasing costs — currently a great deal, because match semantics
are implicit-AND.

### 6. Let the gate arbitrate

For `paraphrase` cases, the query may share at most `MAX_PARAPHRASE_OVERLAP`
stemmed content words with its gold span. Stemming comes from FTS5 itself, so
the gate measures overlap in exactly the token space the index uses.

**A case that fails the gate is re-drawn from another independently generated
candidate — never hand-edited into compliance.** Hand-editing puts one author's
vocabulary back on both sides, which is the thing the whole procedure exists to
prevent.

### 7. Re-record the baseline

The corpus hash changes, so the numbers are no longer comparable to the
recorded ones. Ordinary `just retrieval-update` **refuses** a corpus-hash
mismatch. Use `just retrieval-update-corpus` for an intentional replacement;
that path states that old and new numbers are not directly comparable, and
still refuses to launder an ordinary same-corpus regression.

## Known limits of this slice

- **`episode` has one need (two cases), `dates` and `incidents` two needs.**
  Thin. A single query moving swings those categories hard, so read them as
  directional. RET-06 kept query count flat; distractor density was the
  priority.
- **Paraphrase and buried-evidence still share some target spans** (deliberate
  isolation of the paraphrase penalty against a fixed fact).
- **The fixtures contain one internal inconsistency**: `battery-replacement-dates.md`
  puts an `AWS-11` battery swap in October 2025, while `station-inventory.md`
  installs `AWS-11` in January 2026. Real workspaces contradict themselves and
  the inventory document explicitly claims tie-breaker status, so this was kept
  rather than corrected — but no gold label depends on it.
