"""Tests for FTS5 search (spec-versioning §2.3)."""

from __future__ import annotations

import sqlite3

import pytest

from lattice.core.errors import ValidationError
from lattice.core.paths import WorkspaceRoot
from lattice.core.search import build_match_expression, search_project
from lattice.core.writes import archive_document, create_document


def test_build_match_expression_quotes_terms() -> None:
    assert build_match_expression("bench press") == '"bench" "press"'
    assert build_match_expression('"bench press" heavy') == '"bench press" "heavy"'
    # FTS operators are neutralized by quoting.
    assert build_match_expression("NEAR(x) OR *") == '"NEAR(x)" "OR" "*"'


def test_build_match_expression_rejects_unbalanced_quotes() -> None:
    with pytest.raises(ValidationError, match="Unbalanced"):
        build_match_expression('"unclosed phrase')
    with pytest.raises(ValidationError, match="at least one term"):
        build_match_expression('""')


def _seed(conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str) -> None:
    create_document(
        conn,
        workspace,
        project,
        folder_path="canvases",
        title="Training Log",
        content="Bench pressed 3x8 at 20 kg. Felt strong.\n",
    )
    create_document(
        conn,
        workspace,
        project,
        folder_path="library",
        title="Bench Guide",
        content="A guide about bench pressing form, bench setup, bench arch.\n",
    )
    create_document(
        conn,
        workspace,
        project,
        folder_path="memory",
        title="Unrelated",
        content="Squats only here.\n",
    )


def test_search_returns_ranked_results_with_real_scores(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _seed(conn, workspace, project)
    results = search_project(conn, project, "bench")
    assert [r.path for r in results][:2] == [
        "library/bench-guide.md",
        "canvases/training-log.md",
    ]
    assert all(r.score != 0.0 for r in results)
    assert results[0].score >= results[1].score
    assert "[bench]" in results[0].snippet.lower()


def test_search_stems_via_porter(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _seed(conn, workspace, project)
    # "pressing" stems to match "pressed".
    results = search_project(conn, project, "pressing")
    assert any(r.path == "canvases/training-log.md" for r in results)


def test_search_filters_by_folder_and_status(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _seed(conn, workspace, project)
    results = search_project(conn, project, "bench", folder="library")
    assert [r.path for r in results] == ["library/bench-guide.md"]
    results = search_project(conn, project, "bench", status="active")
    assert len(results) == 2


def test_search_excludes_archived_by_default(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _seed(conn, workspace, project)
    archive_document(conn, workspace, project, path="library/bench-guide.md")
    results = search_project(conn, project, "bench")
    assert [r.path for r in results] == ["canvases/training-log.md"]
    included = search_project(conn, project, "bench", include_archived=True)
    assert {r.path for r in included} == {
        "canvases/training-log.md",
        "archive/library/bench-guide.md",
    }


def test_search_empty_query_returns_nothing(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    assert search_project(conn, project, "   ") == []


def test_search_rejects_invalid_filters(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    with pytest.raises(ValidationError, match="folder"):
        search_project(conn, project, "bench", folder="secrets")
    with pytest.raises(ValidationError, match="status"):
        search_project(conn, project, "bench", status="unknown")
    with pytest.raises(ValidationError, match="limit"):
        search_project(conn, project, "bench", limit=101)
