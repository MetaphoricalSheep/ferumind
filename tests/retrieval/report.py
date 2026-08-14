"""Report mode's output format — which is a deliverable, not an afterthought.

This is what a follow-on retrieval ticket runs before and after its change and
pastes into its own completion evidence, so it is designed to be read in a diff
and to survive a copy-paste into Markdown.

Rates are derived **here** and nowhere else. The baseline stores only integers
(see ``scorer``), so percentages exist for humans and never enter a file that
gets compared byte-for-byte.
"""

from __future__ import annotations

from collections.abc import Sequence

from tests.retrieval.labels import CATEGORIES, PHRASINGS
from tests.retrieval.scorer import Counts, Granularity, RunMetrics

_HEADERS = ("scope", "n", "top1", "top5", "top10", "evid", "bytes")


def _rate(hits: int, total: int) -> str:
    return "  -  " if total == 0 else f"{hits / total:>4.0%} "


def _row(scope: str, counts: Counts, *, granularity: Granularity = "document") -> str:
    """One metric row. At section granularity the top-k columns are section ranks."""
    if counts.queries == 0:
        return f"{scope:<20} {0:>3}      -      -      -      -       -"
    if granularity == "section":
        top1, top5, top10 = counts.section_top1, counts.section_top5, counts.section_top10
    else:
        top1, top5, top10 = counts.document_top1, counts.document_top5, counts.document_top10
    return (
        f"{scope:<20} {counts.queries:>3} "
        f"{_rate(top1, counts.queries)} "
        f"{_rate(top5, counts.queries)} "
        f"{_rate(top10, counts.queries)} "
        f"{_rate(counts.evidence_in_snippet, counts.queries)} "
        f"{counts.payload_bytes:>7}"
    )


def _candidate_line(scope: str, counts: Counts) -> str:
    """Plain-language candidate-generation state for one scope."""
    nonempty = counts.nonempty_without_gold + counts.nonempty_with_gold
    if nonempty == 0:
        conditional = "n/a (no nonempty results)"
    else:
        rate = counts.nonempty_with_gold / nonempty
        conditional = (
            f"gold-hit rate conditional on a non-empty result set: "
            f"{counts.nonempty_with_gold}/{nonempty} ({rate:.0%})"
        )
    return (
        f"  {scope}: {counts.zero_result_queries} returned nothing; "
        f"{nonempty} returned at least one candidate "
        f"({counts.nonempty_with_gold} with gold, "
        f"{counts.nonempty_without_gold} without); "
        f"{counts.results_returned} hits total; {conditional}"
    )


def render(metrics: RunMetrics, *, sqlite_version: str, documents: int) -> str:
    """The full metric table, per phrasing and per category."""
    g = metrics.granularity
    lines: list[str] = [
        "Ferumind retrieval baseline",
        f"  corpus:      {documents} documents, {metrics.total.queries} labelled queries",
        f"  granularity: {g}"
        + (
            "  (section metrics are DEGENERATE — credited whenever the "
            "document matched, because nothing supplies a section yet)"
            if g == "document"
            else "  (top1/5/10 columns are section ranks; document ranks stay in the baseline JSON)"
        ),
        f"  sqlite:      {sqlite_version}",
        "",
        f"{_HEADERS[0]:<20} {_HEADERS[1]:>3} {_HEADERS[2]:>5} {_HEADERS[3]:>6} "
        f"{_HEADERS[4]:>6} {_HEADERS[5]:>6} {_HEADERS[6]:>7}",
        "-" * 60,
        _row("TOTAL", metrics.total, granularity=g),
        "",
        "by phrasing",
    ]
    lines.extend(_row(f"  {p}", metrics.per_phrasing[p], granularity=g) for p in PHRASINGS)
    lines.append("")
    lines.append("by category")
    lines.extend(_row(f"  {c}", metrics.per_category[c], granularity=g) for c in CATEGORIES)
    lines.append("")
    lines.append("candidate generation")
    lines.append(_candidate_line("TOTAL", metrics.total))
    lines.extend(_candidate_line(f"phrasing:{p}", metrics.per_phrasing[p]) for p in PHRASINGS)
    lines.append("")
    lines.extend(_caveats(metrics))
    return "\n".join(lines)


def _caveats(metrics: RunMetrics) -> Sequence[str]:
    """What the numbers do **not** mean. Required by the ticket, and load-bearing.

    A table of percentages invites over-reading. These lines travel with it so
    that a number pasted into a ticket carries its own limits.
    """
    return (
        "what these numbers are not:",
        "  - not a measure of answer quality. Retrieval only: did the evidence come back.",
        "  - not comparable to REL-030's live result_bytes. Different corpus, and bytes",
        "    here exclude the bm25 float and the MCP envelope.",
        "  - not comparable across a corpus change. The baseline records a corpus hash",
        "    and refuses to compare across one; intentional replacement uses",
        "    'just retrieval-update-corpus'.",
        "  - payload bytes and candidate diagnostics are observed, never pass/fail.",
        "    Bytes rise with recall; ratcheting nonempty rates would reward junk hits.",
        (
            "  - section metrics are degenerate at document granularity."
            if metrics.granularity == "document"
            else "  - section metrics are live; they no longer mirror the document ones."
        ),
    )
