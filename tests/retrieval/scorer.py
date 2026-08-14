"""The instrument: labelled results in, integers out.

Two properties drive every decision here.

**It is pure.** The scorer never runs a search; it takes results that have
already been fetched. That is what makes it testable against hand-constructed
inputs with a known answer — an untested instrument makes every number it
produces unfalsifiable.

**It emits only integers.** Counts and byte sizes, never a rate and never a
float that came out of the ranker. Rates are derived at report time. This is not
fastidiousness: SQLite and FTS5 differ across the 3.12/3.13/3.14 matrix, and a
frozen float is exactly the value that would differ while every behaviour stayed
identical. With integers only, "two runs produce byte-identical metrics" is a
property of the design rather than something to hope for.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from tests.retrieval.labels import (
    CATEGORIES,
    PHRASINGS,
    Category,
    Phrasing,
    QueryCase,
    QuerySet,
)

#: The highlight delimiters and separator ``core.search`` passes to FTS5
#: ``snippet()``. Stripped before the evidence substring check, since they are
#: presentation, not content.
_SNIPPET_OPEN: Final = "["
_SNIPPET_CLOSE: Final = "]"
_SNIPPET_ELLIPSIS: Final = " … "

_WHITESPACE = re.compile(r"\s+")

type Granularity = Literal["document", "section"]


@dataclass(frozen=True, slots=True)
class RetrievedResult:
    """One search hit, in the shape the scorer needs.

    ``section_id`` is set for section-aware ``search_project`` hits (RET-03).
    A ``None`` value keeps document-granularity scoring for injected regressions.
    """

    path: str
    snippet: str
    score: float
    section_id: str | None = None


class Counts(BaseModel):
    """Everything recorded for one category, or for the run as a whole."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    queries: int
    document_top1: int
    document_top5: int
    document_top10: int
    section_top1: int
    section_top5: int
    section_top10: int
    evidence_in_snippet: int
    also_relevant_returned: int
    payload_bytes: int
    #: Candidate-generation diagnostics. Partition ``queries`` into zero /
    #: nonempty-without-gold / nonempty-with-gold. Observed only — never
    #: ratcheted (ratcheting nonempty upward would reward junk hits).
    #: Defaults of zero keep a pre-RET-06 baseline file loadable once so
    #: ``--update`` can re-record the fuller shape.
    zero_result_queries: int = 0
    nonempty_without_gold: int = 0
    nonempty_with_gold: int = 0
    #: Sum of result-set sizes across the queries in this scope.
    results_returned: int = 0

    def as_mapping(self) -> Mapping[str, int]:
        """Metric name to value, for code that iterates metrics by name.

        Written out rather than reached through ``getattr``: the ratchet walks
        these by name, and ``getattr`` on a non-literal would hand it an
        untyped value and silently tolerate a metric renamed on one side only.
        """
        return {
            "queries": self.queries,
            "document_top1": self.document_top1,
            "document_top5": self.document_top5,
            "document_top10": self.document_top10,
            "section_top1": self.section_top1,
            "section_top5": self.section_top5,
            "section_top10": self.section_top10,
            "evidence_in_snippet": self.evidence_in_snippet,
            "also_relevant_returned": self.also_relevant_returned,
            "payload_bytes": self.payload_bytes,
            "zero_result_queries": self.zero_result_queries,
            "nonempty_without_gold": self.nonempty_without_gold,
            "nonempty_with_gold": self.nonempty_with_gold,
            "results_returned": self.results_returned,
        }

    def __add__(self, other: Counts) -> Counts:
        return Counts(
            queries=self.queries + other.queries,
            document_top1=self.document_top1 + other.document_top1,
            document_top5=self.document_top5 + other.document_top5,
            document_top10=self.document_top10 + other.document_top10,
            section_top1=self.section_top1 + other.section_top1,
            section_top5=self.section_top5 + other.section_top5,
            section_top10=self.section_top10 + other.section_top10,
            evidence_in_snippet=self.evidence_in_snippet + other.evidence_in_snippet,
            also_relevant_returned=self.also_relevant_returned + other.also_relevant_returned,
            payload_bytes=self.payload_bytes + other.payload_bytes,
            zero_result_queries=self.zero_result_queries + other.zero_result_queries,
            nonempty_without_gold=self.nonempty_without_gold + other.nonempty_without_gold,
            nonempty_with_gold=self.nonempty_with_gold + other.nonempty_with_gold,
            results_returned=self.results_returned + other.results_returned,
        )


ZERO_COUNTS: Final = Counts(
    queries=0,
    document_top1=0,
    document_top5=0,
    document_top10=0,
    section_top1=0,
    section_top5=0,
    section_top10=0,
    evidence_in_snippet=0,
    also_relevant_returned=0,
    payload_bytes=0,
    zero_result_queries=0,
    nonempty_without_gold=0,
    nonempty_with_gold=0,
    results_returned=0,
)


class RunMetrics(BaseModel):
    """A whole run: per category, and in aggregate.

    Reporting per category is the point. An aggregate hides that paraphrase
    collapsed while exact-identifier stayed perfect, which is exactly the
    information a follow-on ticket needs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: ``document`` means section metrics are **degenerate** — credited whenever
    #: the document matched, because nothing supplies a section. Reporting that
    #: honestly is the point; the number becomes meaningful when RET-03 lands.
    granularity: Granularity
    per_category: Mapping[Category, Counts]
    #: The second axis. Kept apart from ``per_category`` because phrasing and
    #: content-kind fail independently. Under strict AND this axis exposed
    #: candidate starvation on natural language; under OR + bm25 it still
    #: separates register effects that an aggregate would hide.
    per_phrasing: Mapping[Phrasing, Counts]
    total: Counts


def rank_deterministically(results: Sequence[RetrievedResult]) -> tuple[RetrievedResult, ...]:
    """Impose a total order before scoring.

    Two documents with identical bm25 scores come back in whatever order SQLite
    produces, so a top-1 metric can flip between runs on nothing but storage
    order. Path is the tiebreak: arbitrary, but stable everywhere.
    """
    return tuple(sorted(results, key=lambda result: (-result.score, result.path)))


def snippet_fragments(snippet: str) -> tuple[str, ...]:
    """The text an agent actually sees, split at the elision points.

    Fragments are compared separately because FTS5 joins disjoint spans with an
    ellipsis; treating the join as continuous text would credit an evidence span
    that straddles material the agent never saw.
    """
    stripped = snippet.replace(_SNIPPET_OPEN, "").replace(_SNIPPET_CLOSE, "")
    return tuple(
        _normalize(fragment) for fragment in stripped.split(_SNIPPET_ELLIPSIS) if fragment.strip()
    )


def evidence_in_snippet(evidence: str, snippet: str) -> bool:
    """Whether the labelled span survived into the returned snippet.

    Fully mechanical — a substring check against a labelled span, no judgement.
    This is the metric that moves when the 12-token window widens or section
    rows land, and without it "top-1 was already correct" hides that the snippet
    was useless and a whole-document read followed anyway.
    """
    needle = _normalize(evidence)
    return any(needle in fragment for fragment in snippet_fragments(snippet))


def payload_bytes(results: Sequence[RetrievedResult]) -> int:
    """Serialised size of a result set, in bytes.

    Deliberately **not** the MCP envelope: measuring through it would couple the
    baseline to unrelated result-shape edits. Equally deliberately, the bm25
    score is excluded — it is a float whose repr can differ across SQLite builds
    while behaviour is identical, and it is not what an agent consumes.

    Comparable within this harness only. It is not comparable to REL-030's live
    ``result_bytes``, which measured a different thing over a different corpus.
    """
    payload = [
        {"path": result.path, "snippet": result.snippet, "section_id": result.section_id}
        for result in results
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return len(encoded.encode("utf-8"))


def score_case(case: QueryCase, results: Sequence[RetrievedResult]) -> Counts:
    """Score one query. *results* need not be pre-sorted."""
    ranked = rank_deterministically(results)
    granularity = detect_granularity(ranked)

    document_rank = _first_rank(ranked, case.gold_paths)
    section_rank = document_rank if granularity == "document" else _first_section_rank(ranked, case)
    zero, without_gold, with_gold = _candidate_state(ranked, case.gold_paths)

    return Counts(
        queries=1,
        document_top1=_credit(document_rank, 1),
        document_top5=_credit(document_rank, 5),
        document_top10=_credit(document_rank, 10),
        section_top1=_credit(section_rank, 1),
        section_top5=_credit(section_rank, 5),
        section_top10=_credit(section_rank, 10),
        evidence_in_snippet=int(_evidence_reached(case, ranked)),
        also_relevant_returned=int(_also_relevant_reached(case, ranked)),
        payload_bytes=payload_bytes(ranked),
        zero_result_queries=zero,
        nonempty_without_gold=without_gold,
        nonempty_with_gold=with_gold,
        results_returned=len(ranked),
    )


def score_run(
    query_set: QuerySet,
    results_by_case: Mapping[str, Sequence[RetrievedResult]],
) -> RunMetrics:
    """Aggregate a whole run, per category and in total.

    Every case in *query_set* must have an entry in *results_by_case*, even an
    empty one. A missing entry is a harness bug, not a zero score, and silently
    treating it as a miss would understate the baseline forever.
    """
    missing = sorted(case.id for case in query_set.cases if case.id not in results_by_case)
    if missing:
        msg = f"No results supplied for query cases: {', '.join(missing)}"
        raise KeyError(msg)

    per_category: dict[Category, Counts] = dict.fromkeys(CATEGORIES, ZERO_COUNTS)
    per_phrasing: dict[Phrasing, Counts] = dict.fromkeys(PHRASINGS, ZERO_COUNTS)
    total = ZERO_COUNTS
    granularity: Granularity = "document"

    for case in query_set.cases:
        results = results_by_case[case.id]
        if detect_granularity(results) == "section":
            granularity = "section"
        counts = score_case(case, results)
        per_category[case.category] = per_category[case.category] + counts
        per_phrasing[case.phrasing] = per_phrasing[case.phrasing] + counts
        total = total + counts

    return RunMetrics(
        granularity=granularity,
        per_category=per_category,
        per_phrasing=per_phrasing,
        total=total,
    )


def detect_granularity(results: Sequence[RetrievedResult]) -> Granularity:
    """``section`` as soon as anything supplies a section id, else ``document``."""
    return "section" if any(result.section_id is not None for result in results) else "document"


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().casefold()


def _credit(rank: int | None, k: int) -> int:
    return int(rank is not None and rank <= k)


def _candidate_state(
    ranked: Sequence[RetrievedResult], gold_paths: frozenset[str]
) -> tuple[int, int, int]:
    """Return ``(zero, nonempty_without_gold, nonempty_with_gold)`` for one case."""
    if not ranked:
        return (1, 0, 0)
    if any(result.path in gold_paths for result in ranked):
        return (0, 0, 1)
    return (0, 1, 0)


def _first_rank(ranked: Sequence[RetrievedResult], paths: frozenset[str]) -> int | None:
    for position, result in enumerate(ranked, start=1):
        if result.path in paths:
            return position
    return None


def _first_section_rank(ranked: Sequence[RetrievedResult], case: QueryCase) -> int | None:
    wanted = {(answer.path, answer.section_id) for answer in case.gold}
    for position, result in enumerate(ranked, start=1):
        if result.section_id is not None and (result.path, result.section_id) in wanted:
            return position
    return None


def _evidence_reached(case: QueryCase, ranked: Sequence[RetrievedResult]) -> bool:
    """Did any returned snippet carry a labelled span for the document it hit?"""
    spans_by_path: dict[str, list[str]] = {}
    for answer in case.gold:
        spans_by_path.setdefault(answer.path, []).append(answer.evidence)
    return any(
        evidence_in_snippet(span, result.snippet)
        for result in ranked
        for span in spans_by_path.get(result.path, ())
    )


def _also_relevant_reached(case: QueryCase, ranked: Sequence[RetrievedResult]) -> bool:
    """Whether the superseded/companion document came back alongside the gold one.

    Only meaningful where the case declares one — chiefly false-premise cases,
    where an agent shown *only* the superseded document answers confidently from
    a false premise. Cases without ``also_relevant`` never contribute.
    """
    if not case.also_relevant:
        return False
    returned = {result.path for result in ranked}
    return bool(set(case.also_relevant) & returned) and bool(case.gold_paths & returned)
