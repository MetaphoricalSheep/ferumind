"""Fixtures for the retrieval harness.

The corpus workspace is **session-scoped**: bootstrapping a workspace, creating
a project and indexing seventeen documents costs far more than the searches run
against it. A fixture per test would make the harness cost proportional to how
thoroughly it checks — the exact pressure that stops people adding checks.

Nothing here touches a configured or live workspace; every path comes from
``tmp_path_factory``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.retrieval.corpus import QUERIES_PATH, CorpusWorkspace, build_corpus_workspace
from tests.retrieval.labels import QuerySet, load_query_set
from tests.retrieval.stemming import Stemmer


@pytest.fixture(scope="session")
def query_set() -> QuerySet:
    return load_query_set(QUERIES_PATH)


@pytest.fixture(scope="session")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CorpusWorkspace]:
    workspace_parent: Path = tmp_path_factory.mktemp("retrieval-corpus")
    built = build_corpus_workspace(workspace_parent / "workspace")
    yield built
    built.close()


@pytest.fixture(scope="session")
def stemmer() -> Iterator[Stemmer]:
    with Stemmer() as instance:
        yield instance
