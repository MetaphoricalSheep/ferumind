"""Full-text search backed by SQLite FTS5 (product/spec-versioning.md §2.3).

The raw user query is sanitized into an FTS match expression: double-quoted
spans become phrases, everything else is split into individually quoted
terms (implicit AND). Raw FTS syntax is never injected; unbalanced quotes
raise ``VALIDATION_ERROR`` rather than a 500. Ranking is bm25 (exposed
negated so higher is better) and snippets come from FTS5 ``snippet()``.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from lattice.core.errors import ValidationError
from lattice.core.folders import ROLE_FOLDERS
from lattice.core.frontmatter import ALLOWED_STATUSES
from lattice.core.types import DbConnection, JsonObject, JsonValue

MAX_SEARCH_QUERY_CHARS = 1024
MAX_SEARCH_TERMS = 100
MAX_SEARCH_RESULTS = 100
_SEARCH_FOLDERS = frozenset({"spine", *ROLE_FOLDERS})


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    title: str
    folder: str
    status: str
    edit_policy: str
    snippet: str
    #: Negated bm25 rank: higher is a better match.
    score: float


def build_match_expression(query: str) -> str:
    """Sanitize a raw user query into a safe FTS5 match expression."""
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
    return " ".join(terms)


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
    """Search indexed documents within a project.

    Archived documents (``status: archived`` or living under ``archive/``)
    are excluded unless *include_archived* is set (00 D6).
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

    clauses = ["si.project_key = ?", "search_index MATCH ?"]
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
    # above; all caller-controlled values remain bound parameters.
    query_sql = f"""SELECT si.path, si.title, d.folder, d.status, d.edit_policy,
                   snippet(search_index, 1, '[', ']', ' … ', 12) AS snip,
                   bm25(search_index) AS rank
            FROM search_index si
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
            edit_policy=row["edit_policy"],
            snippet=row["snip"],
            score=-float(row["rank"]),
        )
        for row in rows
    ]
