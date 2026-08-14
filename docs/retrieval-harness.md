# The retrieval evaluation harness

```bash
just retrieval-report            # the metric table (~2s)
just retrieval-update            # re-record the baseline; refuses on a regression
just retrieval-update-corpus     # intentional corpus replacement (non-comparable)
uv run pytest tests/retrieval    # assertion mode, the same thing pytest runs
```

`tests/retrieval/` measures whether search surfaces the **right evidence** for a
query, against a synthetic corpus with hand-labelled gold answers.

## Why it exists

Section-aware retrieval changed the search architecture twice. Before this
harness there was no way to tell whether such a change made retrieval better or
merely different. The `RET-nn` labels below name the internal work items each
baseline was recorded under; they are chronological markers for the numbers,
not links.

The observation log cannot answer it: it records what agents *did* — tools
called, bytes moved, follow-up reads — and nothing about whether search returned
the right document. REL-030 tried a live before/after anyway and got a
clean-looking result that turned out to be a confound: 99.9% of the bytes came
from one project's photo-triage week, which ended between the two windows. The
workload moved, so the measurement moved.

That is the argument for a fixed corpus in one sentence: **live-usage
measurement moved because the work moved, and a benchmark that cannot hold the
workload still cannot attribute a change to the code.**

## What is measured

Retrieval, not answer generation. Whether a model writes a good answer from the
evidence is out of scope and unmeasured.

| Metric | What it says |
|---|---|
| `document_top1/5/10` | Did the labelled document come back, and how high |
| `evidence_in_snippet` | Did the labelled **span** survive into the returned snippet |
| `section_top1/5/10` | Same, at section resolution |
| `payload_bytes` | Serialised size of the result set. **Observed, never pass/fail** |
| `zero_result_queries` / `nonempty_without_gold` / `nonempty_with_gold` | Candidate-generation state. **Observed, never pass/fail** |
| `results_returned` | Sum of hit counts. **Observed, never pass/fail** |

Two axes are reported separately: **category** (what the query asks about) and
**phrasing** (`natural` versus `keyword`). They fail independently, and folding
them together would let a collapse in one hide behind the other.

`evidence_in_snippet` is the metric that matters most for what comes next. A
correct top-1 with a useless snippet still forces a whole-document read — which
is exactly REL-030's 5.1× finding — and only this metric can see that.

Candidate diagnostics separate **recall** from **ranking**: a zero-result query
never consulted the ranker; a nonempty result without gold is a ranking miss;
a nonempty result with gold is a candidate-generation success. Report mode
prints these in plain language. The useful conditional figure is the
**gold-hit rate conditional on a non-empty result set** — never “precision”.

## The baseline, and what it says

Recorded in `retrieval-baseline.json`.

- **RET-01 (historical):** 17 docs, strict AND — top-1 23%, top-5/10 27%,
  evidence 1%, zero-result 46/70.
- **RET-06 (historical, same AND, expanded corpus):** 40 docs — top-1 20%,
  top-5/10 31%, evidence 1%, zero-result 40/70, gold|nonempty 22/30 (73%).
  Not comparable to RET-01 (corpus change).
- **RET-05 (historical, OR + bm25, document-level, same 40-doc corpus):**

| scope | top-1 | top-5 | top-10 | evidence |
|---|---|---|---|---|
| **total** | 27% | 84% | 91% | 3% |
| natural phrasing | 29% | 83% | 89% | 0% |
| keyword phrasing | 26% | 86% | 94% | 6% |

Candidate generation: **0** zero-result; **70** nonempty (**64** with gold,
**6** without). Gold-hit rate conditional on a non-empty result set:
**64/70 (91%)**.

**What moved under RET-05 was candidate recall.** Non-empty rate 30/70 → 70/70;
conditional gold-hit 73% → 91%. That match contract (OR + bm25) is unchanged.

- **RET-03 (current, section-aware, heading×2 bm25, 24-token snippets):**

| scope | doc top-1 | doc top-5 | doc top-10 | sec top-1 | sec top-5 | sec top-10 | evidence |
|---|---|---|---|---|---|---|---|
| **total** | 37% (26) | 74% (52) | 87% (61) | 27% (19) | 50% (35) | 69% (48) | **34% (24)** |

Candidate generation: **0** zero-result; **70** nonempty (**61** with gold,
**9** without). Gold|nonempty: **61/70 (87%)**. Payload bytes: 108552 → 173249
(+60%).

**What moved under RET-03 is evidence usability and section resolution.**
Evidence-in-snippet rose from 2/70 (3%) to 24/70 (34%) — the metric this
ticket exists to move. Document top-1 improved (19→26). Document top-5/top-10
and also_relevant fell (59→52, 64→61, 15→12): under a fixed hit budget of 10,
several sections from one document crowd out other documents. That is the
measured cost of section granularity, not a ranking bug; accepted and locked.
gold|nonempty 64→61 is the expected narrower-match-scope cost under OR.

bm25 weights chosen against the harness: title=1.0, heading=2.0, body=1.0.
Snippet window: **24** tokens (12→3 evidence; 24→24 evidence; wider windows
bought diminishing returns at rising payload).

Section metrics are live: the baseline records `granularity: section`, so a
section hit is scored at section resolution rather than credited whenever the
document matched. They were degenerate under the earlier document-level
baselines, which is why the transition is one-way in the ratchet below.

## Regression semantics

A ratchet, following `scripts/complexity_ratchet.py`:

- a **regression** fails;
- an **improvement** also fails, as a stale baseline, until re-recorded —
  otherwise a resolved gap leaves a permanent licence to reintroduce itself;
- `--update` / `just retrieval-update` refuses while a regression stands, so
  it cannot launder one in;
- a **corpus hash change** fails on the ordinary path rather than silently
  comparing different things;
- intentional corpus replacement uses `just retrieval-update-corpus`
  (`--accept-corpus-change`). That path states that old and new numbers are
  **not directly comparable**. On an unchanged corpus it still refuses an
  ordinary retrieval regression — accepting a corpus change accepts
  non-comparability, not worse retrieval on the same corpus;
- the one-way **document → section** granularity transition skips section
  metrics that were previously degenerate, while still ratcheting document
  top-k, evidence, and `also_relevant_returned`. Once the baseline itself
  reads `granularity: section`, section metrics ratchet normally.
  `section → document` is a capability regression, not a transition.

There are no fixed pass marks. A hardcoded target is either so loose it never
fires or needs editing on every improvement, and in both cases it stops meaning
anything.

## Determinism

Three hazards, each handled rather than hoped away.

**bm25 ties.** Results are sorted `(-score, path)` before scoring, so identical
scores cannot leave top-1 to SQLite's storage order. Proven by a test that
flattens every score to a constant.

**SQLite across the matrix.** Measured, not assumed: the baseline is
**byte-identical** on managed 3.12, 3.13 and 3.14 (SQLite 3.53.1) *and* on
system 3.12 (SQLite 3.45.1). One baseline therefore suffices. The recorded
`sqlite_version` is advisory — it is named in any failure it could explain, and
never fails on its own.

**Latency.** Recorded in report mode only, never in the baseline. A flaky
retrieval test gets disabled within a month and then the harness is decorative.

The baseline stores **only integers** — counts and byte sizes, never a rate and
never a float from the ranker. Rates are derived at report time. That makes
"two runs produce byte-identical metrics" a property of the design rather than
something to hope for.

## Operator mode

Runs read-only against your real workspace.

```bash
uv run python scripts/retrieval_report.py --operator --queries ~/private/queries.txt
```

Every guard fails closed: the workspace comes from `FERUMIND_WORKSPACE` and
cannot be passed as an argument; the database is opened `mode=ro` at the SQLite
level rather than by discipline; it refuses under `CI`; it refuses a query or
output path that Git tracks or that sits un-ignored inside the repository; and
the report carries aggregate counts only — no path, query text, snippet or
title, so pasting it into a ticket cannot paste your workspace with it.

A stale index is *reported*, never reconciled — reconciling would write.

One measured caveat: a `mode=ro` connection to a WAL database still creates
`-shm`/`-wal` sidecars, because WAL readers need the shared-memory index. That
is SQLite's documented behaviour and not a write to user data; the tests assert
no document, operation or snapshot changes rather than pretending a read leaves
zero filesystem trace.

## What these numbers do not mean

- **Not answer quality.** Retrieval only.
- **Not comparable to REL-030's live `result_bytes`.** Different corpus, and
  bytes here exclude the bm25 float and the MCP envelope.
- **Not comparable across a corpus change.** The hash guard refuses; intentional
  replacement uses `just retrieval-update-corpus` and states non-comparability.
- **Not a response budget.** `payload_bytes` here is an observation and must
  not grow into an enforcement point; a real ingress budget is separate work.
- **Candidate diagnostics are observations.** Ratcheting nonempty rates upward
  would reward junk hits.

## Adding a case

See [tests/fixtures/retrieval/README.md](../tests/fixtures/retrieval/README.md).
The short version: write the intent first, have the query written by someone who
has not seen the document, label at `(path, section, span)` resolution, and let
the overlap gate arbitrate rather than hand-editing a case into compliance.
After fixtures or labels change, re-record with `just retrieval-update-corpus`.
