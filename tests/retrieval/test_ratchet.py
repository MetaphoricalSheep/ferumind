"""The ratchet's contract, including the parts that must refuse.

A guard never shown to fail is not known to work. This repository has been bitten
by exactly that twice — REL-021's silently-failing-open observation wrapper and
REL-033's silent-green matrix — so every arm here is exercised, especially the
ones whose job is to say no.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.retrieval.labels import CATEGORIES, PHRASINGS, Category, Phrasing
from tests.retrieval.ratchet import (
    CORPUS_ACCEPT_MESSAGE,
    Baseline,
    BaselineMetadata,
    baseline_from,
    compare,
    corpus_fingerprint,
    format_failure,
    load_baseline,
    update_baseline,
    write_baseline,
)
from tests.retrieval.scorer import Counts, Granularity, RunMetrics

UPDATE_COMMAND = "uv run python scripts/retrieval_report.py --update"


def _counts(
    *,
    queries: int = 10,
    top1: int = 5,
    evidence: int = 3,
    payload: int = 1000,
    also_relevant: int = 1,
    zero_result_queries: int = 0,
    nonempty_without_gold: int = 0,
    nonempty_with_gold: int | None = None,
    results_returned: int = 20,
) -> Counts:
    with_gold = top1 if nonempty_with_gold is None else nonempty_with_gold
    return Counts(
        queries=queries,
        document_top1=top1,
        document_top5=top1 + 2,
        document_top10=top1 + 3,
        section_top1=top1,
        section_top5=top1 + 2,
        section_top10=top1 + 3,
        evidence_in_snippet=evidence,
        also_relevant_returned=also_relevant,
        payload_bytes=payload,
        zero_result_queries=zero_result_queries,
        nonempty_without_gold=nonempty_without_gold,
        nonempty_with_gold=with_gold,
        results_returned=results_returned,
    )


def _baseline(
    total: Counts | None = None,
    *,
    per_category: dict[Category, Counts] | None = None,
    sqlite_version: str = "3.53.1",
    corpus_sha256: str = "abc123",
    granularity: Granularity = "document",
) -> Baseline:
    resolved_total = total if total is not None else _counts()
    categories: dict[Category, Counts] = per_category or dict.fromkeys(
        CATEGORIES,
        _counts(
            queries=0,
            top1=0,
            evidence=0,
            payload=0,
            also_relevant=0,
            zero_result_queries=0,
            nonempty_without_gold=0,
            nonempty_with_gold=0,
            results_returned=0,
        ),
    )
    phrasings: dict[Phrasing, Counts] = dict.fromkeys(PHRASINGS, resolved_total)
    return Baseline(
        metadata=BaselineMetadata(
            sqlite_version=sqlite_version,
            corpus_sha256=corpus_sha256,
            granularity=granularity,
            query_count=resolved_total.queries,
        ),
        per_category=categories,
        per_phrasing=phrasings,
        total=resolved_total,
    )


class TestRegressionDetection:
    def test_a_drop_in_top1_is_a_regression(self) -> None:
        result = compare(_baseline(_counts(top1=5)), _baseline(_counts(top1=3)))
        assert result.regressions
        assert not result.improvements
        assert "document_top1 fell from 5 to 3" in result.regressions[0]

    def test_a_drop_in_evidence_rate_is_a_regression(self) -> None:
        """The metric section retrieval exists to move must be guarded too."""
        result = compare(_baseline(_counts(evidence=8)), _baseline(_counts(evidence=2)))
        assert any("evidence_in_snippet fell from 8 to 2" in line for line in result.regressions)

    def test_payload_growth_is_observed_but_never_fails(self) -> None:
        """Bytes rise with recall, so enforcing them would punish the wins.

        A search that starts returning the right document *as well as* what it
        already returned is better and bigger at once. Payload size is an
        observation here and must not become an enforcement point; a real
        response budget belongs at the ingress, not in the harness.
        """
        result = compare(_baseline(_counts(payload=1000)), _baseline(_counts(payload=4000)))
        assert result.is_clean
        assert any("payload_bytes grew from 1000 to 4000" in line for line in result.observations)

    def test_the_failing_category_is_named(self) -> None:
        """AC6: the message must say *which* category dropped, not merely that one did."""
        recorded: dict[Category, Counts] = dict.fromkeys(CATEGORIES, _counts())
        measured: dict[Category, Counts] = dict(recorded)
        measured["paraphrase"] = _counts(top1=0)

        result = compare(_baseline(per_category=recorded), _baseline(per_category=measured))
        message = format_failure(result, update_command=UPDATE_COMMAND)
        assert "paraphrase: document_top1 fell from 5 to 0" in message
        assert "identifiers" not in message


class TestStaleBaseline:
    def test_an_improvement_fails_until_re_recorded(self) -> None:
        """Otherwise a resolved gap leaves a licence to reintroduce itself."""
        result = compare(_baseline(_counts(top1=3)), _baseline(_counts(top1=7)))
        assert result.improvements
        assert not result.regressions
        assert not result.is_clean

    def test_payload_shrinking_is_observed_but_never_stales_the_baseline(self) -> None:
        result = compare(_baseline(_counts(payload=4000)), _baseline(_counts(payload=1000)))
        assert result.is_clean
        assert any("payload_bytes shrank from 4000 to 1000" in line for line in result.observations)

    def test_the_stale_message_points_at_update(self) -> None:
        result = compare(_baseline(_counts(top1=3)), _baseline(_counts(top1=7)))
        assert UPDATE_COMMAND in format_failure(result, update_command=UPDATE_COMMAND)

    def test_a_regression_message_forecloses_laundering_it(self) -> None:
        result = compare(_baseline(_counts(top1=7)), _baseline(_counts(top1=3)))
        message = format_failure(result, update_command=UPDATE_COMMAND)
        assert "--update refuses while a regression stands" in message


class TestIdentity:
    def test_identical_numbers_are_clean(self) -> None:
        assert compare(_baseline(), _baseline()).is_clean

    def test_a_changed_corpus_is_not_silently_compared(self) -> None:
        """Numbers from a different corpus are not comparable; passing would be worse."""
        result = compare(_baseline(corpus_sha256="aaa"), _baseline(corpus_sha256="bbb"))
        assert any("describe a different corpus" in line for line in result.regressions)

    def test_a_changed_query_count_is_named(self) -> None:
        result = compare(_baseline(_counts(queries=10)), _baseline(_counts(queries=12)))
        assert any("the query set changed" in line for line in result.regressions)

    def test_sqlite_difference_is_a_note_not_a_failure(self) -> None:
        """Hazard 2: flag it as a candidate explanation, never fail on it alone."""
        result = compare(_baseline(sqlite_version="3.45.1"), _baseline(sqlite_version="3.53.1"))
        assert result.is_clean
        assert any("3.45.1" in note and "3.53.1" in note for note in result.notes)

    def test_sqlite_note_accompanies_a_real_failure(self) -> None:
        result = compare(
            _baseline(_counts(top1=5), sqlite_version="3.45.1"),
            _baseline(_counts(top1=1), sqlite_version="3.53.1"),
        )
        message = format_failure(result, update_command=UPDATE_COMMAND)
        assert "document_top1 fell from 5 to 1" in message
        assert "may be environmental" in message


class TestSerialisation:
    def test_round_trip_is_byte_identical(self, tmp_path: Path) -> None:
        """Determinism starts here: the same baseline must serialise the same way."""
        path = tmp_path / "retrieval-baseline.json"
        write_baseline(path, _baseline())
        first = path.read_bytes()
        write_baseline(path, load_baseline(path))
        assert path.read_bytes() == first

    def test_a_missing_baseline_says_how_to_make_one(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="--update"):
            load_baseline(tmp_path / "absent.json")

    def test_baseline_from_metrics_carries_the_provenance(self) -> None:
        metrics = RunMetrics(
            granularity="document",
            per_category=dict.fromkeys(CATEGORIES, _counts()),
            per_phrasing=dict.fromkeys(PHRASINGS, _counts()),
            total=_counts(queries=35),
        )
        baseline = baseline_from(metrics, sqlite_version="3.53.1", corpus_sha256="deadbeef")
        assert baseline.metadata.query_count == 35
        assert baseline.metadata.sqlite_version == "3.53.1"
        assert baseline.metadata.corpus_sha256 == "deadbeef"


class TestCorpusFingerprint:
    def test_content_change_moves_the_digest(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        (corpus / "library").mkdir(parents=True)
        document = corpus / "library" / "a.md"
        document.write_text("original", encoding="utf-8")
        queries = tmp_path / "queries.yaml"
        queries.write_text("[]", encoding="utf-8")

        before = corpus_fingerprint(corpus, queries)
        document.write_text("edited", encoding="utf-8")
        assert corpus_fingerprint(corpus, queries) != before

    def test_label_change_moves_the_digest(self, tmp_path: Path) -> None:
        """A relabelled query changes the answer key, so the numbers change meaning."""
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "a.md").write_text("body", encoding="utf-8")
        queries = tmp_path / "queries.yaml"
        queries.write_text("[]", encoding="utf-8")

        before = corpus_fingerprint(corpus, queries)
        queries.write_text("- id: q-1", encoding="utf-8")
        assert corpus_fingerprint(corpus, queries) != before

    def test_digest_is_stable_across_repeated_reads(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "a.md").write_text("body", encoding="utf-8")
        queries = tmp_path / "queries.yaml"
        queries.write_text("[]", encoding="utf-8")
        assert corpus_fingerprint(corpus, queries) == corpus_fingerprint(corpus, queries)


class TestGranularityTransition:
    def test_document_to_section_can_establish_the_first_section_baseline(
        self, tmp_path: Path
    ) -> None:
        """Problem A: section metrics are non-comparable across the transition."""
        recorded = _baseline(
            _counts(top1=5, evidence=3),
            granularity="document",
        )
        # Section metrics fall (stricter), document metrics hold.
        current = _baseline(
            Counts(
                queries=10,
                document_top1=5,
                document_top5=7,
                document_top10=8,
                section_top1=2,
                section_top5=3,
                section_top10=4,
                evidence_in_snippet=3,
                also_relevant_returned=1,
                payload_bytes=1000,
                zero_result_queries=0,
                nonempty_without_gold=0,
                nonempty_with_gold=5,
                results_returned=20,
            ),
            granularity="section",
        )
        result = compare(recorded, current)
        assert not any("section_top" in line for line in result.regressions)
        assert result.is_clean
        path = tmp_path / "baseline.json"
        write_baseline(path, recorded)
        update_baseline(path, current)
        assert load_baseline(path).metadata.granularity == "section"

    def test_a_document_regression_still_fails_during_the_transition(self) -> None:
        recorded = _baseline(_counts(top1=5), granularity="document")
        current = _baseline(
            Counts(
                queries=10,
                document_top1=3,
                document_top5=5,
                document_top10=6,
                section_top1=1,
                section_top5=2,
                section_top10=3,
                evidence_in_snippet=3,
                also_relevant_returned=1,
                payload_bytes=1000,
                zero_result_queries=0,
                nonempty_without_gold=0,
                nonempty_with_gold=3,
                results_returned=20,
            ),
            granularity="section",
        )
        result = compare(recorded, current)
        assert any("document_top1 fell from 5 to 3" in line for line in result.regressions)
        assert not any("section_top" in line for line in result.regressions)

    def test_section_metrics_ratchet_once_the_baseline_is_section(self) -> None:
        recorded = _baseline(_counts(top1=5), granularity="section")
        current = _baseline(
            Counts(
                queries=10,
                document_top1=5,
                document_top5=7,
                document_top10=8,
                section_top1=2,
                section_top5=4,
                section_top10=5,
                evidence_in_snippet=3,
                also_relevant_returned=1,
                payload_bytes=1000,
                zero_result_queries=0,
                nonempty_without_gold=0,
                nonempty_with_gold=5,
                results_returned=20,
            ),
            granularity="section",
        )
        result = compare(recorded, current)
        assert any("section_top1 fell from 5 to 2" in line for line in result.regressions)

    def test_an_improvement_still_requires_a_deliberate_refresh(self) -> None:
        recorded = _baseline(_counts(top1=3), granularity="document")
        current = _baseline(_counts(top1=7), granularity="section")
        result = compare(recorded, current)
        assert result.improvements
        assert not result.is_clean

    def test_section_to_document_is_a_capability_regression(self) -> None:
        result = compare(
            _baseline(granularity="section"),
            _baseline(granularity="document"),
        )
        assert any("fell from section to document" in line for line in result.regressions)


class TestCorpusChangeAcceptance:
    def test_ordinary_update_refuses_a_corpus_hash_mismatch(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        write_baseline(path, _baseline(corpus_sha256="aaa"))
        before = path.read_bytes()
        with pytest.raises(RuntimeError, match="describe a different corpus"):
            update_baseline(path, _baseline(corpus_sha256="bbb"))
        assert path.read_bytes() == before

    def test_accept_corpus_change_records_a_new_baseline(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        write_baseline(path, _baseline(_counts(top1=8), corpus_sha256="aaa"))
        current = _baseline(_counts(top1=2), corpus_sha256="bbb")
        comparison = update_baseline(path, current, accept_corpus_change=True)
        assert load_baseline(path).metadata.corpus_sha256 == "bbb"
        assert CORPUS_ACCEPT_MESSAGE in comparison.notes
        assert any("not directly comparable" in note for note in comparison.notes)

    def test_accept_corpus_change_refuses_a_same_corpus_regression(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        write_baseline(path, _baseline(_counts(top1=8), corpus_sha256="same"))
        before = path.read_bytes()
        with pytest.raises(RuntimeError, match="refuses while a regression stands"):
            update_baseline(
                path,
                _baseline(_counts(top1=2), corpus_sha256="same"),
                accept_corpus_change=True,
            )
        assert path.read_bytes() == before


class TestObservedDiagnostics:
    def test_candidate_diagnostic_deltas_are_never_regressions(self) -> None:
        recorded = _baseline(
            _counts(zero_result_queries=2, nonempty_with_gold=8, results_returned=20)
        )
        current = _baseline(
            _counts(zero_result_queries=9, nonempty_with_gold=1, results_returned=5)
        )
        result = compare(recorded, current)
        assert result.is_clean
        assert any("zero_result_queries" in line for line in result.observations)
        assert not any("zero_result_queries" in line for line in result.regressions)
        assert not any("nonempty_with_gold" in line for line in result.improvements)
