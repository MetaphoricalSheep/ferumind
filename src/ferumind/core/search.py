"""Full-text search backed by SQLite FTS5 (product/spec-versioning.md §2.3).

The raw user query is sanitized into an FTS match expression: double-quoted
spans become phrases, everything else is split into individually quoted
terms joined with ``OR``. Raw FTS syntax is never injected; unbalanced quotes
raise ``VALIDATION_ERROR`` rather than a 500. Ranking is bm25 (exposed
negated so higher is better) and snippets come from FTS5 ``snippet()``.

OR + bm25 is deliberate (RET-05): strict AND starved candidate generation —
natural-language questions zeroed out whenever any function word was absent —
so ranking never ran. Any term may match; bm25 sorts discriminative hits above
common ones.

Hits are **sections** (RET-03), not whole documents. Each row carries the
line range an agent can hand straight to ``read_document_range``. Ranking
weights (title / heading / body) and the snippet token window were chosen
against the RET-01 harness on the post-RET-05 corpus — see RET-03 for the
measured table.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from ferumind.core.errors import ValidationError
from ferumind.core.folders import ROLE_FOLDERS
from ferumind.core.frontmatter import ALLOWED_STATUSES
from ferumind.core.types import DbConnection, JsonObject, JsonValue

MAX_SEARCH_QUERY_CHARS = 1024
MAX_SEARCH_TERMS = 100
MAX_SEARCH_RESULTS = 100

#: FTS5 ``snippet()`` token window. Chosen on the RET-01 harness (post-RET-05
#: corpus): 12 → evidence-in-snippet 3/70; 24 → 24/70 at +60% payload bytes;
#: 32 did not improve evidence further; 40/48/64 bought diminishing returns.
SNIPPET_TOKENS = 24

#: bm25 column weights for ``section_index`` (title, heading, body). Heading
#: weight 2.0 beat equal weighting on document top-1 (25→26) and section
#: top-5 (33→35) without hurting gold|nonempty (61/70). Higher heading
#: weights (4-8) and down-weighting body traded away top-5/top-10 recall.
BM25_WEIGHT_TITLE = 1.0
BM25_WEIGHT_HEADING = 2.0
BM25_WEIGHT_BODY = 1.0

#: ``snippet(section_index, …)`` column index for ``body`` (0=title, 1=heading).
_SNIPPET_BODY_COLUMN = 2

_SEARCH_FOLDERS = frozenset({"spine", *ROLE_FOLDERS})


class SearchResult(BaseModel):
    """One section-level search hit.

    ``edit_policy`` is intentionally absent: it is document-level and would
    repeat on every section of the same document. Agents that need it get it
    from ``read_document`` / propose echoes, not from every search row.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    title: str
    folder: str
    status: str
    section_id: str
    kind: Literal["preamble", "heading"]
    heading_text: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    level: int | None = None
    start_line: int
    end_line: int
    #: UTF-8 byte size of the section text — a cheap read-cost hint.
    size_bytes: int
    snippet: str
    #: Negated bm25 rank: higher is a better match.
    score: float


def build_match_expression(query: str) -> str:
    """Sanitize a raw user query into a safe FTS5 match expression.

    Terms are individually quoted (so raw FTS operators cannot be injected) and
    joined with ``OR``. A section matches if it contains **any** term; bm25
    ranks multi-term and rare-term hits above weak ones.
    """
    if len(query) > MAX_SEARCH_QUERY_CHARS:
        raise ValidationError(f"Search query exceeds the {MAX_SEARCH_QUERY_CHARS}-character limit")
    if query.count('"') % 2 != 0:
        raise ValidationError(
            "Unbalanced double quotes in search query",
            details={"query": query},
        )
    terms: list[str] = []
    remainder = query
    while '"' in remainder:
        before, _, rest = remainder.partition('"')
        phrase, _, remainder = rest.partition('"')
        terms.extend(_split_terms(before))
        phrase = phrase.strip()
        if phrase:
            terms.append(_quote(phrase))
    terms.extend(_split_terms(remainder))
    if not terms:
        raise ValidationError("Search query must contain at least one term")
    if len(terms) > MAX_SEARCH_TERMS:
        raise ValidationError(f"Search query exceeds the {MAX_SEARCH_TERMS}-term limit")
    return " OR ".join(terms)


def _split_terms(text: str) -> list[str]:
    return [_quote(term) for term in text.split() if term.strip('"')]


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def search_project(
    conn: DbConnection,
    project_key: str,
    query: str,
    *,
    folder: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 20,
) -> Sequence[SearchResult]:
    """Search indexed Markdown **sections** within a project.

    ``limit`` bounds sections, not documents — several sections from one
    document may appear as separate hits. Archived documents (``status:
    archived`` or living under ``archive/``) are excluded unless
    *include_archived* is set (00 D6).
    """
    if folder is not None and folder not in _SEARCH_FOLDERS:
        allowed_folders: list[JsonValue] = []
        allowed_folders.extend(sorted(_SEARCH_FOLDERS))
        details: JsonObject = {"allowed_folders": allowed_folders}
        raise ValidationError(
            f"Unknown search folder {folder!r}",
            details=details,
        )
    if status is not None and status not in ALLOWED_STATUSES:
        allowed_statuses: list[JsonValue] = []
        allowed_statuses.extend(sorted(ALLOWED_STATUSES))
        details = {"allowed_statuses": allowed_statuses}
        raise ValidationError(
            f"Unknown document status {status!r}",
            details=details,
        )
    if limit < 1 or limit > MAX_SEARCH_RESULTS:
        raise ValidationError(f"limit must be between 1 and {MAX_SEARCH_RESULTS}")
    if not query.strip():
        return []
    match_expr = build_match_expression(query)

    clauses = ["si.project_key = ?", "section_index MATCH ?"]
    params: list[object] = [project_key, match_expr]
    if folder is not None:
        clauses.append("d.folder = ?")
        params.append(folder)
    if status is not None:
        clauses.append("d.status = ?")
        params.append(status)
    if not include_archived:
        clauses.append("d.status != 'archived'")
        clauses.append("d.folder != 'archive'")
    params.append(limit)

    # S608: the interpolated clauses are selected only by the fixed branches
    # above; weights/snippet width are module constants; all caller-controlled
    # values remain bound parameters.
    query_sql = f"""SELECT si.path, si.title, d.folder, d.status,
                   si.section_id, si.kind, si.heading_text, si.heading_path_json,
                   si.level, si.start_line, si.end_line, si.size_bytes,
                   snippet(section_index, {_SNIPPET_BODY_COLUMN}, '[', ']', ' … ',
                           {SNIPPET_TOKENS}) AS snip,
                   bm25(section_index, {BM25_WEIGHT_TITLE}, {BM25_WEIGHT_HEADING},
                        {BM25_WEIGHT_BODY}) AS rank
            FROM section_index si
            JOIN documents d ON d.project_key = si.project_key AND d.path = si.path
            WHERE {" AND ".join(clauses)}
            ORDER BY rank
            LIMIT ?"""  # noqa: S608
    rows = conn.execute(query_sql, tuple(params)).fetchall()

    return [
        SearchResult(
            path=row["path"],
            title=row["title"],
            folder=row["folder"],
            status=row["status"],
            section_id=row["section_id"],
            kind=row["kind"],
            heading_text=row["heading_text"],
            heading_path=_heading_path(row["heading_path_json"]),
            level=_optional_int(row["level"]),
            start_line=int(row["start_line"]),
            end_line=int(row["end_line"]),
            size_bytes=int(row["size_bytes"]),
            snippet=row["snip"] or "",
            score=-float(row["rank"]),
        )
        for row in rows
    ]


def _heading_path(raw: object) -> list[str]:
    if raw is None or raw == "":
        return []
    parsed: object = json.loads(str(raw))
    if not isinstance(parsed, list):
        return []
    items = cast(list[object], parsed)
    return [str(item) for item in items]


def _optional_int(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float, str)):
        return int(raw)
    msg = f"expected int-compatible value, got {type(raw).__name__}"
    raise TypeError(msg)
