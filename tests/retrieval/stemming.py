"""Porter-stemmed token sets, taken from FTS5 itself.

The overlap gate exists to catch a paraphrase fixture that quietly shares
vocabulary with the document it is supposed to be a paraphrase *of*. To do that
honestly it has to measure sharing in the **same token space the index uses** —
a second stemmer would agree today and disagree in a year, and the gate would
then be measuring something search does not do.

So it asks SQLite. A temporary ``fts5`` table declared with the index's own
``tokenize = 'porter unicode61'`` (see ``db/schema.sql``) plus an ``fts5vocab``
view over it yields exactly the terms the real index would store. No new
dependency, and no possibility of drift.

One correction FTS5 does not make for us: its porter tokenizer **stems** function
words rather than dropping them, so ``the``, ``is`` and ``did`` all survive into
the term list. Counting those would report a shared stem between any two English
sentences, so :data:`STOPWORDS` removes them before overlap is measured.
"""

from __future__ import annotations

import sqlite3
from types import TracebackType
from typing import Final, Self

#: Function words FTS5 keeps but that carry no topical meaning. Deliberately
#: small: this list exists to stop the gate reporting a false overlap on "the",
#: not to implement information retrieval. Anything with topical content — even
#: a vague word like "problem" — stays in, because a paraphrase that reuses it
#: really has reused vocabulary.
STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "about",
        "after",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "before",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "down",
        "for",
        "from",
        "get",
        "got",
        "had",
        "has",
        "have",
        "he",
        "her",
        "here",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "me",
        "more",
        "most",
        "much",
        "my",
        "no",
        "not",
        "now",
        "of",
        "off",
        "on",
        "one",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "still",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "thi",
        "this",
        "those",
        "through",
        "to",
        "too",
        "up",
        "us",
        "use",
        "used",
        "veri",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)


class Stemmer:
    """Tokenise text exactly as ``search_index`` would.

    Holds one in-memory SQLite connection; construction is cheap but not free,
    so callers scoring a whole corpus should build one and reuse it. Usable as a
    context manager.
    """

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute("CREATE VIRTUAL TABLE t USING fts5(txt, tokenize='porter unicode61')")
        self._conn.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, 'row')")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def tokens(self, text: str) -> frozenset[str]:
        """Every distinct stem FTS5 would index for *text*, stopwords included."""
        self._conn.execute("DELETE FROM t")
        self._conn.execute("INSERT INTO t(txt) VALUES (?)", (text,))
        return frozenset(str(row[0]) for row in self._conn.execute("SELECT term FROM v"))

    def content_tokens(self, text: str) -> frozenset[str]:
        """:meth:`tokens` with function words removed — the gate's unit."""
        return frozenset(self.tokens(text) - STOPWORDS)

    def shared_content_tokens(self, left: str, right: str) -> frozenset[str]:
        """Content stems occurring in both texts.

        This is the number the paraphrase gate thresholds on: a query and the
        gold span it targets should have almost nothing in common here, or the
        case is a lexical match wearing a paraphrase label.
        """
        return frozenset(self.content_tokens(left) & self.content_tokens(right))
