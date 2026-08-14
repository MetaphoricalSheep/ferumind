"""Instrument tests: hand-built results with a known answer.

The scorer is the thing every number in the baseline comes out of. If it is
wrong, the baseline is confidently wrong and nothing downstream can tell. So it
is exercised here against inputs whose correct score is obvious by inspection,
never against whatever search happens to return.
"""

from __future__ import annotations

import pytest

from tests.retrieval.labels import GoldAnswer, QueryCase, QuerySet
from tests.retrieval.scorer import (
    RetrievedResult,
    evidence_in_snippet,
    payload_bytes,
    rank_deterministically,
    score_case,
    score_run,
    snippet_fragments,
)


def _hit(
    path: str, score: float, snippet: str = "", section_id: str | None = None
) -> RetrievedResult:
    return RetrievedResult(path=path, snippet=snippet, score=score, section_id=section_id)


def _case(
    *,
    case_id: str = "q-001",
    path: str = "library/a.md",
    section_id: str = "intro",
    evidence: str = "the answer",
    also_relevant: tuple[str, ...] = (),
) -> QueryCase:
    return QueryCase(
        id=case_id,
        category="stable-facts",
        phrasing="natural",
        query="anything",
        gold=(GoldAnswer(path=path, section_id=section_id, evidence=evidence),),
        also_relevant=also_relevant,
    )


class TestDeterministicRanking:
    def test_ties_break_by_path_not_storage_order(self) -> None:
        """Identical bm25 scores must not leave top-1 to SQLite's whim."""
        shuffled = [_hit("z.md", 1.0), _hit("a.md", 1.0), _hit("m.md", 1.0)]
        assert [r.path for r in rank_deterministically(shuffled)] == ["a.md", "m.md", "z.md"]

    def test_higher_score_still_wins(self) -> None:
        ranked = rank_deterministically([_hit("a.md", 1.0), _hit("z.md", 9.0)])
        assert [r.path for r in ranked] == ["z.md", "a.md"]

    def test_ranking_is_idempotent(self) -> None:
        once = rank_deterministically([_hit("b.md", 2.0), _hit("a.md", 2.0)])
        assert rank_deterministically(once) == once


class TestSnippetHandling:
    def test_highlight_markers_are_not_content(self) -> None:
        assert snippet_fragments("the [rain] gauge") == ("the rain gauge",)

    def test_elided_spans_stay_separate(self) -> None:
        assert snippet_fragments("first part … second part") == ("first part", "second part")

    def test_evidence_found_across_whitespace_and_case(self) -> None:
        assert evidence_in_snippet("The Answer", "… and so   the\nanswer is that …")

    def test_evidence_straddling_an_elision_does_not_count(self) -> None:
        """The agent never saw the join, so crediting it would flatter the metric."""
        assert not evidence_in_snippet("first second", "first … second")

    def test_absent_evidence_is_absent(self) -> None:
        assert not evidence_in_snippet("the answer", "something else entirely")


class TestPayloadBytes:
    def test_known_serialisation_size(self) -> None:
        """Hand-counted: [{"path":"a.md","section_id":null,"snippet":"x"}]"""
        assert payload_bytes([_hit("a.md", 1.0, snippet="x")]) == 49

    def test_bm25_score_is_excluded(self) -> None:
        """A float whose repr can differ across SQLite builds must not move bytes."""
        assert payload_bytes([_hit("a.md", 1.0, "x")]) == payload_bytes(
            [_hit("a.md", 3.14159265358979, "x")]
        )

    def test_empty_result_set_is_two_bytes(self) -> None:
        assert payload_bytes([]) == 2


class TestScoreCase:
    def test_gold_at_rank_one_credits_every_k(self) -> None:
        counts = score_case(_case(), [_hit("library/a.md", 5.0), _hit("other.md", 1.0)])
        assert (counts.document_top1, counts.document_top5, counts.document_top10) == (1, 1, 1)

    def test_gold_at_rank_six_credits_only_top_ten(self) -> None:
        results = [_hit(f"filler-{i}.md", 10.0 - i) for i in range(5)]
        results.append(_hit("library/a.md", 1.0))
        counts = score_case(_case(), results)
        assert (counts.document_top1, counts.document_top5, counts.document_top10) == (0, 0, 1)

    def test_gold_beyond_rank_ten_credits_nothing(self) -> None:
        results = [_hit(f"filler-{i:02d}.md", 100.0 - i) for i in range(12)]
        results.append(_hit("library/a.md", 0.5))
        counts = score_case(_case(), results)
        assert (counts.document_top1, counts.document_top5, counts.document_top10) == (0, 0, 0)

    def test_missing_gold_scores_zero_but_still_counts_the_query(self) -> None:
        counts = score_case(_case(), [_hit("wrong.md", 5.0)])
        assert counts.queries == 1
        assert counts.document_top10 == 0

    def test_evidence_credited_only_from_the_document_that_holds_it(self) -> None:
        """A span appearing in the wrong document is not evidence."""
        counts = score_case(_case(), [_hit("wrong.md", 9.0, snippet="the answer")])
        assert counts.evidence_in_snippet == 0

    def test_evidence_credited_when_the_snippet_carries_it(self) -> None:
        counts = score_case(_case(), [_hit("library/a.md", 9.0, snippet="… the answer …")])
        assert counts.evidence_in_snippet == 1

    def test_correct_document_with_a_useless_snippet_scores_the_split(self) -> None:
        """The exact failure the evidence metric exists to expose.

        Top-1 is perfect and the agent still has to read the whole document,
        because the twelve-token window returned nothing usable.
        """
        counts = score_case(_case(), [_hit("library/a.md", 9.0, snippet="… unrelated text …")])
        assert counts.document_top1 == 1
        assert counts.evidence_in_snippet == 0


class TestCandidateDiagnostics:
    def test_empty_results_are_zero_result(self) -> None:
        counts = score_case(_case(), [])
        assert counts.zero_result_queries == 1
        assert counts.nonempty_without_gold == 0
        assert counts.nonempty_with_gold == 0
        assert counts.results_returned == 0

    def test_nonempty_with_gold(self) -> None:
        counts = score_case(_case(), [_hit("library/a.md", 9.0), _hit("other.md", 1.0)])
        assert counts.zero_result_queries == 0
        assert counts.nonempty_with_gold == 1
        assert counts.nonempty_without_gold == 0
        assert counts.results_returned == 2

    def test_nonempty_without_gold(self) -> None:
        counts = score_case(_case(), [_hit("wrong.md", 9.0)])
        assert counts.nonempty_without_gold == 1
        assert counts.nonempty_with_gold == 0
        assert counts.results_returned == 1

    def test_the_three_states_partition_queries(self) -> None:
        cases = (
            _case(case_id="q-zero"),
            _case(case_id="q-gold", path="library/a.md"),
            _case(case_id="q-miss", path="library/b.md"),
        )
        metrics = score_run(
            QuerySet(cases=cases),
            {
                "q-zero": [],
                "q-gold": [_hit("library/a.md", 9.0)],
                "q-miss": [_hit("wrong.md", 9.0)],
            },
        )
        assert metrics.total.queries == 3
        assert metrics.total.zero_result_queries == 1
        assert metrics.total.nonempty_with_gold == 1
        assert metrics.total.nonempty_without_gold == 1
        assert (
            metrics.total.zero_result_queries
            + metrics.total.nonempty_with_gold
            + metrics.total.nonempty_without_gold
            == metrics.total.queries
        )


class TestAlsoRelevant:
    def test_both_documents_returned_is_credited(self) -> None:
        case = _case(also_relevant=("archive/old.md",))
        counts = score_case(case, [_hit("library/a.md", 9.0), _hit("archive/old.md", 8.0)])
        assert counts.also_relevant_returned == 1

    def test_superseded_document_alone_is_not_credited(self) -> None:
        """An agent shown only the stale document answers a false premise confidently."""
        case = _case(also_relevant=("archive/old.md",))
        counts = score_case(case, [_hit("archive/old.md", 9.0)])
        assert counts.also_relevant_returned == 0

    def test_cases_without_a_companion_never_contribute(self) -> None:
        counts = score_case(_case(), [_hit("library/a.md", 9.0)])
        assert counts.also_relevant_returned == 0


class TestSectionGranularity:
    def test_document_level_results_report_degenerate_section_metrics(self) -> None:
        """Today nothing supplies a section, so section credit mirrors document credit."""
        counts = score_case(_case(), [_hit("library/a.md", 9.0)])
        assert counts.section_top1 == counts.document_top1 == 1

    def test_section_ids_make_the_metric_discriminate(self) -> None:
        """The moment RET-03 lands: right document, wrong section, no section credit."""
        results = [_hit("library/a.md", 9.0, section_id="conclusion")]
        counts = score_case(_case(section_id="intro"), results)
        assert counts.document_top1 == 1
        assert counts.section_top1 == 0

    def test_right_section_credits_both(self) -> None:
        results = [_hit("library/a.md", 9.0, section_id="intro")]
        counts = score_case(_case(section_id="intro"), results)
        assert counts.document_top1 == 1
        assert counts.section_top1 == 1


class TestScoreRun:
    def test_aggregates_per_category_and_in_total(self) -> None:
        cases = (
            _case(case_id="q-1", path="library/a.md"),
            QueryCase(
                id="q-2",
                category="paraphrase",
                phrasing="natural",
                query="anything",
                gold=(GoldAnswer(path="library/b.md", section_id="intro", evidence="x"),),
            ),
        )
        metrics = score_run(
            QuerySet(cases=cases),
            {"q-1": [_hit("library/a.md", 9.0)], "q-2": [_hit("wrong.md", 9.0)]},
        )
        assert metrics.total.queries == 2
        assert metrics.total.document_top1 == 1
        assert metrics.per_category["stable-facts"].document_top1 == 1
        assert metrics.per_category["paraphrase"].document_top1 == 0
        assert metrics.per_category["episode"].queries == 0

    def test_a_missing_case_is_an_error_not_a_zero(self) -> None:
        """Silently scoring a harness bug as a miss would understate the baseline forever."""
        with pytest.raises(KeyError, match="q-1"):
            score_run(QuerySet(cases=(_case(case_id="q-1"),)), {})

    def test_granularity_reported_as_document_today(self) -> None:
        metrics = score_run(
            QuerySet(cases=(_case(case_id="q-1"),)), {"q-1": [_hit("library/a.md", 9.0)]}
        )
        assert metrics.granularity == "document"

    def test_scoring_is_order_independent(self) -> None:
        """Same results, different input order, identical metrics."""
        case = _case(case_id="q-1")
        forward = [_hit("library/a.md", 5.0), _hit("z.md", 5.0)]
        reverse = list(reversed(forward))
        assert score_case(case, forward) == score_case(case, reverse)
