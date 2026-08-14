"""Loading the synthetic corpus into a real, throwaway workspace.

The corpus is built through the **ordinary** paths — ``bootstrap``,
``create_project``, ``index_project`` — rather than by writing rows into
SQLite directly. That costs a little speed and buys two things: the harness
measures the real parser and the real indexer rather than a mock, and a corpus
that fails to index is a genuine finding rather than a silent zero.

``create_project`` seeds ``spine.md`` and ``rules/00-project.md`` into every
project. They are indexed alongside the fixtures and no gold label points at
them — they are distractors, which is what they are in a real project too.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from ferumind.core.indexer import index_project, project_dir_for
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.project_writes import create_project
from ferumind.core.search import search_project
from ferumind.db.database import Database
from tests.retrieval.labels import QueryCase, QuerySet
from tests.retrieval.scorer import RetrievedResult, RunMetrics, score_run

FIXTURE_ROOT: Final = Path(__file__).resolve().parent.parent / "fixtures" / "retrieval"
CORPUS_ROOT: Final = FIXTURE_ROOT / "corpus"
QUERIES_PATH: Final = FIXTURE_ROOT / "queries.yaml"

PROJECT_KEY: Final = "ardwell-weather"
PROJECT_TITLE: Final = "Ardwell Valley weather network"

#: What an agent actually sees. Ten is the largest k the harness scores, so
#: fetching more would inflate the payload-size metric with results no metric
#: reads.
SEARCH_LIMIT: Final = 10


class CorpusIndexError(RuntimeError):
    """The fixture corpus failed to index. Always a real defect, never a score."""


@dataclass(frozen=True, slots=True)
class CorpusWorkspace:
    """A bootstrapped workspace holding the indexed corpus."""

    workspace: WorkspaceRoot
    database: Database
    connection: sqlite3.Connection
    project_key: str
    documents_indexed: int

    def close(self) -> None:
        self.connection.close()


class SearchFn(Protocol):
    """The seam the injected-regression test replaces.

    Scoring never calls search directly; it calls one of these. That is what
    lets a deliberately degraded implementation be substituted and the ratchet
    shown to fail — a guard never demonstrated failing is not known to work.
    """

    def __call__(
        self, corpus: CorpusWorkspace, case: QueryCase, /, *, limit: int = SEARCH_LIMIT
    ) -> Sequence[RetrievedResult]: ...


def build_corpus_workspace(
    destination: Path,
    *,
    corpus_root: Path = CORPUS_ROOT,
    project_key: str = PROJECT_KEY,
) -> CorpusWorkspace:
    """Bootstrap a workspace at *destination*, install the corpus, index it.

    *destination* must not already exist as a workspace; callers pass a
    ``tmp_path``. Nothing here ever touches a configured or live workspace.
    """
    if not corpus_root.is_dir():
        msg = f"Corpus fixtures not found at {corpus_root}"
        raise CorpusIndexError(msg)

    # Imported here rather than at module scope: ``scripts/`` is not a package
    # and only conftest puts it on the path.
    from bootstrap_workspace import bootstrap

    bootstrap(destination, force=False)
    workspace = WorkspaceRoot(destination)

    database = Database(destination / ".ferumind" / "ferumind.sqlite")
    database.init_schema()
    connection = database.get_connection()

    create_project(connection, workspace, key=project_key, title=PROJECT_TITLE)
    project_dir = project_dir_for(destination, project_key)
    shutil.copytree(corpus_root, project_dir, dirs_exist_ok=True)

    result = index_project(connection, destination, project_key)
    if result.errors:
        connection.close()
        msg = f"Corpus failed to index: {'; '.join(result.error_messages)}"
        raise CorpusIndexError(msg)

    return CorpusWorkspace(
        workspace=workspace,
        database=database,
        connection=connection,
        project_key=project_key,
        documents_indexed=result.documents_indexed,
    )


def run_harness(
    corpus: CorpusWorkspace,
    query_set: QuerySet,
    search: SearchFn | None = None,
) -> RunMetrics:
    """Run every labelled query and score the outcome.

    *search* defaults to the shipped implementation. Passing a degraded one is
    how the injected-regression test proves the ratchet actually fires.
    """
    engine = search if search is not None else real_search
    results = {case.id: engine(corpus, case) for case in query_set.cases}
    return score_run(query_set, results)


def real_search(
    corpus: CorpusWorkspace, case: QueryCase, *, limit: int = SEARCH_LIMIT
) -> Sequence[RetrievedResult]:
    """Run the shipped ``search_project`` and adapt its rows for the scorer.

    Section ids come through so section metrics discriminate; the document→
    section ratchet transition in ``ratchet.compare`` skips comparing the
    previously degenerate section counts on the first real baseline.
    """
    results = search_project(
        corpus.connection,
        corpus.project_key,
        case.query,
        include_archived=case.include_archived,
        limit=limit,
    )
    return tuple(
        RetrievedResult(
            path=row.path,
            snippet=row.snippet,
            score=row.score,
            section_id=row.section_id,
        )
        for row in results
    )
