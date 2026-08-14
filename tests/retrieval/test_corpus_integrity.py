"""Corpus integrity: what stops the fixtures rotting.

A silently broken label makes a query permanently unanswerable and quietly
depresses the baseline forever — the harness would go on reporting a number
while measuring less than it claims. These tests are what make that loud.

They also enforce the two rules the corpus exists under: everything is
synthetic, and a paraphrase case must not share vocabulary with the span it
targets.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ferumind.core.document_map import (
    derive_sections,
    frontmatter_line_range,
    split_document_lines,
)
from ferumind.core.indexer import project_dir_for
from tests.retrieval.corpus import CORPUS_ROOT, CorpusWorkspace
from tests.retrieval.labels import (
    CATEGORIES,
    MAX_PARAPHRASE_OVERLAP,
    PHRASINGS,
    QuerySet,
)
from tests.retrieval.stemming import Stemmer

_WHITESPACE = re.compile(r"\s+")

#: Anything shaped like a credential trips secret scanning once the repository
#: is public, wasting a human's time on a fixture that was never a secret. The
#: rule is that plausible fakes are as unwelcome as real ones.
_FORBIDDEN = (
    re.compile(r"\bapi[_-]?key\b", re.IGNORECASE),
    re.compile(r"\bsecret\b", re.IGNORECASE),
    re.compile(r"\bpassword\b", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._-]{8,}", re.IGNORECASE),
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{8,}"),
)

#: Real-looking hosts and addresses. ``.invalid`` is reserved by RFC 2606 and
#: can never resolve, so it is the one permitted form.
_HOSTLIKE = re.compile(r"\b[a-z0-9-]+\.(?:com|net|org|io|co|dev|local|internal)\b", re.IGNORECASE)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().casefold()


def _corpus_files() -> list[Path]:
    return sorted(CORPUS_ROOT.rglob("*.md"))


class TestLabelsResolve:
    """Every gold label must point at something that really exists."""

    def test_every_gold_path_is_a_corpus_document(
        self, query_set: QuerySet, corpus: CorpusWorkspace
    ) -> None:
        project_dir = project_dir_for(corpus.workspace, corpus.project_key)
        for case in query_set.cases:
            for answer in case.gold:
                assert (project_dir / answer.path).is_file(), (
                    f"{case.id}: gold path {answer.path!r} does not exist"
                )

    def test_every_gold_section_is_a_derived_section(
        self, query_set: QuerySet, corpus: CorpusWorkspace
    ) -> None:
        """Section ids are not stable across edits, so this is the tripwire.

        Renaming a heading in a fixture silently orphans its label; without this
        test the query would go on being scored against a section that no longer
        exists and would simply never be answerable.
        """
        project_dir = project_dir_for(corpus.workspace, corpus.project_key)
        for case in query_set.cases:
            for answer in case.gold:
                content = (project_dir / answer.path).read_text(encoding="utf-8")
                lines = split_document_lines(content)
                _, body_start = frontmatter_line_range(content)
                ids = {s.section_id for s in derive_sections(lines, body_start, len(lines))}
                assert answer.section_id in ids, (
                    f"{case.id}: section {answer.section_id!r} not in {answer.path} "
                    f"(has: {sorted(ids)})"
                )

    def test_every_evidence_span_occurs_in_its_section(
        self, query_set: QuerySet, corpus: CorpusWorkspace
    ) -> None:
        """The span must be inside the *labelled section*, not merely the document.

        Checking only the document would let a label drift into the wrong
        section unnoticed, and the section-level metric would then be scoring
        against a lie the moment RET-03 makes it meaningful.
        """
        project_dir = project_dir_for(corpus.workspace, corpus.project_key)
        for case in query_set.cases:
            for answer in case.gold:
                content = (project_dir / answer.path).read_text(encoding="utf-8")
                lines = split_document_lines(content)
                _, body_start = frontmatter_line_range(content)
                section = next(
                    s
                    for s in derive_sections(lines, body_start, len(lines))
                    if s.section_id == answer.section_id
                )
                body = _normalize("\n".join(lines[section.start_line - 1 : section.end_line]))
                assert _normalize(answer.evidence) in body, (
                    f"{case.id}: evidence not found in {answer.path}#{answer.section_id}: "
                    f"{answer.evidence!r}"
                )

    def test_every_also_relevant_path_exists(
        self, query_set: QuerySet, corpus: CorpusWorkspace
    ) -> None:
        project_dir = project_dir_for(corpus.workspace, corpus.project_key)
        for case in query_set.cases:
            for path in case.also_relevant:
                assert (project_dir / path).is_file(), f"{case.id}: {path!r} does not exist"

    def test_case_ids_are_unique(self, query_set: QuerySet) -> None:
        ids = [case.id for case in query_set.cases]
        assert len(ids) == len(set(ids))


class TestCoverage:
    def test_every_category_is_represented(self, query_set: QuerySet) -> None:
        """A category lost in an edit must fail loudly, not quietly stop being measured."""
        empty = [c for c in CATEGORIES if not query_set.by_category(c)]
        assert empty == [], f"categories with no queries: {empty}"

    def test_every_phrasing_is_represented(self, query_set: QuerySet) -> None:
        empty = [p for p in PHRASINGS if not query_set.by_phrasing(p)]
        assert empty == [], f"phrasings with no queries: {empty}"

    def test_both_phrasings_cover_the_same_needs(self, query_set: QuerySet) -> None:
        """The phrasing gap is only interpretable if both ask the same things."""

        def needs(phrasing: str) -> set[str]:
            return {
                case.id.rsplit("-", 1)[0] for case in query_set.cases if case.phrasing == phrasing
            }

        assert needs("natural") == needs("keyword")

    def test_archived_content_is_present_and_targeted(self, query_set: QuerySet) -> None:
        """Archive-exclusion-by-default is only testable if the corpus has archived docs."""
        archived = [f for f in _corpus_files() if f.relative_to(CORPUS_ROOT).parts[0] == "archive"]
        assert archived, "corpus has no archive/ documents"
        assert [c for c in query_set.cases if c.include_archived]

    def test_documents_span_every_role_folder(self) -> None:
        """RET-03's folder filter needs fixtures in more than one folder."""
        folders = {f.relative_to(CORPUS_ROOT).parts[0] for f in _corpus_files()}
        assert {"canvases", "library", "memory", "rules", "archive"} <= folders

    def test_a_non_active_status_exists(self) -> None:
        """RET-03's status filter needs something other than ``active`` to filter on."""
        statuses = {
            line.split(":", 1)[1].strip()
            for f in _corpus_files()
            for line in f.read_text(encoding="utf-8").splitlines()[:12]
            if line.startswith("status:")
        }
        assert statuses - {"active"}, f"every fixture is active: {statuses}"


class TestParaphraseGate:
    """The mechanical half of the blind authoring procedure.

    Blinding is a process claim and cannot be verified after the fact. This can:
    a paraphrase query that shares vocabulary with its gold span is a lexical
    match wearing a paraphrase label, whoever wrote it and however carefully.
    """

    def test_paraphrase_queries_do_not_reuse_their_evidence_vocabulary(
        self, query_set: QuerySet, stemmer: Stemmer
    ) -> None:
        offenders: list[str] = []
        for case in query_set.by_category("paraphrase"):
            span = " ".join(answer.evidence for answer in case.gold)
            shared = stemmer.shared_content_tokens(case.query, span)
            if len(shared) > MAX_PARAPHRASE_OVERLAP:
                offenders.append(f"{case.id}: shares {sorted(shared)} with its gold span")
        assert offenders == [], (
            "paraphrase cases must not share vocabulary with the span they target. "
            "Re-draw from the other independently generated candidates rather than "
            "hand-editing, which reintroduces one author's vocabulary:\n  " + "\n  ".join(offenders)
        )


class TestPublicTreeSafety:
    """Everything is synthetic, and must look it to a secret scanner too."""

    @pytest.mark.parametrize("path", _corpus_files(), ids=lambda p: p.name)
    def test_no_credential_shaped_strings(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        for pattern in _FORBIDDEN:
            assert not pattern.search(text), f"{path.name} matches {pattern.pattern!r}"

    @pytest.mark.parametrize("path", _corpus_files(), ids=lambda p: p.name)
    def test_no_resolvable_hostnames_or_addresses(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        assert not _HOSTLIKE.search(text), f"{path.name} contains a real-looking hostname"
        assert not _EMAIL.search(text), f"{path.name} contains an email address"

    def test_the_corpus_is_not_empty(self) -> None:
        """Guards against a silently empty fixture tree passing everything above."""
        assert len(_corpus_files()) >= 15
