"""The overlap gate's tokeniser must match the index's, or it measures nothing."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.retrieval.stemming import STOPWORDS, Stemmer


@pytest.fixture
def stemmer() -> Iterator[Stemmer]:
    with Stemmer() as instance:
        yield instance


def test_inflections_collapse_to_one_stem(stemmer: Stemmer) -> None:
    """Porter folds inflected forms together, which is why the gate uses it.

    A paraphrase saying "calibrating" against a span saying "calibration" has
    reused the word; a raw string comparison would not notice.
    """
    assert stemmer.tokens("calibration") == stemmer.tokens("calibrating")
    assert stemmer.tokens("running") == stemmer.tokens("runs")


def test_function_words_survive_fts5_and_are_removed_here(stemmer: Stemmer) -> None:
    """FTS5's porter stems function words rather than dropping them.

    This is the whole reason :data:`STOPWORDS` exists. Without it every pair of
    English sentences would share several "content" stems and the gate would be
    permanently saturated.
    """
    raw = stemmer.tokens("why did the readings stop")
    assert "the" in raw
    assert "did" in raw

    content = stemmer.content_tokens("why did the readings stop")
    assert content == {"read", "stop"}


def test_stopword_list_uses_stemmed_forms(stemmer: Stemmer) -> None:
    """Entries must be what FTS5 *produces*, not what a human would type.

    ``this`` is indexed as ``thi``; listing only ``this`` would let the real
    token straight through.
    """
    produced = stemmer.tokens("this is very much the same")
    leaked = produced - STOPWORDS
    assert leaked == set(), f"stopwords listed in unstemmed form leak through: {sorted(leaked)}"


def test_shared_content_tokens_ignores_function_words(stemmer: Stemmer) -> None:
    query = "why is the water getting into the boxes"
    unrelated = "the masts are set at two metres above the ground"
    assert stemmer.shared_content_tokens(query, unrelated) == frozenset()


def test_shared_content_tokens_catches_a_lexical_paraphrase(stemmer: Stemmer) -> None:
    """The case the gate exists to reject.

    A "paraphrase" written by whoever wrote the document reuses its nouns, and
    the case becomes an easy lexical match wearing a paraphrase label.
    """
    query = "how do we stop condensation forming in the enclosures"
    span = "condensation forms inside the enclosures whenever the lid is cold"
    shared = stemmer.shared_content_tokens(query, span)
    assert {"condens", "enclosur"} <= shared
