"""Assertion mode: the recorded baseline, and proof the guard actually fires.

This is the test that runs on every commit. It does three jobs, and the second
and third matter as much as the first:

1. compare the measured numbers against ``retrieval-baseline.json``;
2. show the comparison **failing** on a deliberately degraded search, because a
   guard never demonstrated failing is not known to work;
3. show ``--update`` refusing while a regression stands, so an improvement can
   never be used to launder a regression into the baseline.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.retrieval.corpus import (
    CORPUS_ROOT,
    QUERIES_PATH,
    SEARCH_LIMIT,
    CorpusWorkspace,
    SearchFn,
    real_search,
    run_harness,
)
from tests.retrieval.labels import QueryCase, QuerySet
from tests.retrieval.ratchet import (
    BASELINE_PATH,
    UPDATE_COMMAND,
    Baseline,
    baseline_from,
    compare,
    corpus_fingerprint,
    format_failure,
    load_baseline,
    update_baseline,
    write_baseline,
)
from tests.retrieval.scorer import RetrievedResult


def _measure(
    corpus: CorpusWorkspace, query_set: QuerySet, search: SearchFn | None = None
) -> Baseline:
    metrics = run_harness(corpus, query_set, search)
    return baseline_from(
        metrics,
        sqlite_version=sqlite3.sqlite_version,
        corpus_sha256=corpus_fingerprint(CORPUS_ROOT, QUERIES_PATH),
    )


# ── degraded implementations, used to prove the guard fires ──────────────────


def truncating_search(
    corpus: CorpusWorkspace, case: QueryCase, *, limit: int = SEARCH_LIMIT
) -> Sequence[RetrievedResult]:
    """Return only the first hit. Models a ranking that lost its tail."""
    return tuple(real_search(corpus, case, limit=limit))[:1]


def snippetless_search(
    corpus: CorpusWorkspace, case: QueryCase, *, limit: int = SEARCH_LIMIT
) -> Sequence[RetrievedResult]:
    """Correct documents, useless snippets. Models a snippet window regression."""
    return tuple(
        RetrievedResult(path=r.path, snippet="", score=r.score, section_id=r.section_id)
        for r in real_search(corpus, case, limit=limit)
    )


def reversed_search(
    corpus: CorpusWorkspace, case: QueryCase, *, limit: int = SEARCH_LIMIT
) -> Sequence[RetrievedResult]:
    """Invert the ranking. Models bm25 wired up backwards."""
    hits = tuple(real_search(corpus, case, limit=limit))
    return tuple(
        RetrievedResult(path=r.path, snippet=r.snippet, score=-r.score, section_id=r.section_id)
        for r in hits
    )


class TestRecordedBaseline:
    def test_measured_metrics_match_the_recorded_baseline(
        self, corpus: CorpusWorkspace, query_set: QuerySet
    ) -> None:
        recorded = load_baseline(BASELINE_PATH)
        result = compare(recorded, _measure(corpus, query_set))
        assert result.is_clean, format_failure(result, update_command=UPDATE_COMMAND)


class TestDeterminism:
    def test_two_runs_are_byte_identical(
        self, corpus: CorpusWorkspace, query_set: QuerySet, tmp_path: Path
    ) -> None:
        """Not merely equal — identical once serialised, which is what gets committed."""
        first, second = tmp_path / "a.json", tmp_path / "b.json"
        write_baseline(first, _measure(corpus, query_set))
        write_baseline(second, _measure(corpus, query_set))
        assert first.read_bytes() == second.read_bytes()

    def test_an_engineered_bm25_tie_does_not_flip_the_score(
        self, corpus: CorpusWorkspace, query_set: QuerySet
    ) -> None:
        """Hazard 1: identical scores must not leave top-1 to storage order.

        Every score is flattened to a constant, so SQLite's ordering is the only
        thing left to vary. The deterministic path tiebreak has to absorb it.
        """

        def flat_search(
            c: CorpusWorkspace, case: QueryCase, *, limit: int = SEARCH_LIMIT
        ) -> Sequence[RetrievedResult]:
            return tuple(
                RetrievedResult(path=r.path, snippet=r.snippet, score=1.0, section_id=r.section_id)
                for r in real_search(c, case, limit=limit)
            )

        first = run_harness(corpus, query_set, flat_search)
        second = run_harness(corpus, query_set, flat_search)
        assert first == second


class TestInjectedRegression:
    """AC6: demonstrated, not asserted."""

    def test_a_truncated_result_set_fails_the_ratchet(
        self, corpus: CorpusWorkspace, query_set: QuerySet
    ) -> None:
        recorded = load_baseline(BASELINE_PATH)
        result = compare(recorded, _measure(corpus, query_set, truncating_search))
        assert result.regressions

    def test_the_failure_message_names_the_metric_and_the_scope(
        self, corpus: CorpusWorkspace, query_set: QuerySet
    ) -> None:
        """A message saying only "something got worse" would be useless to a follow-on ticket."""
        recorded = load_baseline(BASELINE_PATH)
        message = format_failure(
            compare(recorded, _measure(corpus, query_set, truncating_search)),
            update_command=UPDATE_COMMAND,
        )
        assert "document_top5 fell from" in message
        assert "total:" in message

    def test_losing_the_snippet_is_caught_even_though_ranking_is_perfect(
        self, corpus: CorpusWorkspace, query_set: QuerySet
    ) -> None:
        """The split the evidence metric exists for: top-k unchanged, evidence gone.

        Without this metric a snippet regression would be completely invisible —
        every top-k count stays exactly where it was.
        """
        recorded = load_baseline(BASELINE_PATH)
        degraded = _measure(corpus, query_set, snippetless_search)
        assert degraded.total.document_top1 == recorded.total.document_top1
        result = compare(recorded, degraded)
        assert any("evidence_in_snippet fell" in line for line in result.regressions)

    def test_an_inverted_ranking_fails(self, corpus: CorpusWorkspace, query_set: QuerySet) -> None:
        recorded = load_baseline(BASELINE_PATH)
        result = compare(recorded, _measure(corpus, query_set, reversed_search))
        assert result.regressions


class TestUpdateRefusal:
    def test_update_refuses_while_a_regression_stands(
        self, corpus: CorpusWorkspace, query_set: QuerySet, tmp_path: Path
    ) -> None:
        """The contract that stops --update laundering a regression into the baseline."""
        path = tmp_path / "retrieval-baseline.json"
        write_baseline(path, load_baseline(BASELINE_PATH))
        before = path.read_bytes()

        with pytest.raises(RuntimeError, match="refuses while a regression stands"):
            update_baseline(path, _measure(corpus, query_set, truncating_search))

        assert path.read_bytes() == before, "the baseline must be left untouched"

    def test_update_records_a_genuine_improvement(
        self, corpus: CorpusWorkspace, query_set: QuerySet, tmp_path: Path
    ) -> None:
        path = tmp_path / "retrieval-baseline.json"
        write_baseline(path, _measure(corpus, query_set, truncating_search))
        update_baseline(path, _measure(corpus, query_set))
        assert compare(load_baseline(path), _measure(corpus, query_set)).is_clean
