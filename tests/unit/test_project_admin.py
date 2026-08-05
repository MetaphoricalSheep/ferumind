"""Tests for whole-project admin operations and stale-state cleanup."""

from __future__ import annotations

import sqlite3

import pytest

from ferumind.core.errors import ProjectNotFoundError, ValidationError
from ferumind.core.indexer import rebuild_index
from ferumind.core.operations import WORKSPACE_OPERATION_PROJECT, record_operation
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.project_admin import delete_project, list_all_projects
from ferumind.core.registry import load_registry


def test_list_all_projects_reports_registered_project(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rebuild_index(conn, workspace, [project])

    summaries = list_all_projects(conn, workspace)

    assert [s.key for s in summaries] == [project]
    summary = summaries[0]
    assert summary.title == "Demo"
    assert summary.in_registry is True
    assert summary.folder_exists is True
    assert summary.in_database is True


def test_list_all_projects_surfaces_orphaned_folder_and_db_rows(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rebuild_index(conn, workspace, [project])

    # Simulate the user's scenario: the registry entry is gone (or was
    # never there) but the folder and/or DB rows linger.
    from ferumind.core.registry import remove_registry_entry

    remove_registry_entry(workspace, project)

    summaries = list_all_projects(conn, workspace)

    assert [s.key for s in summaries] == [project]
    summary = summaries[0]
    assert summary.in_registry is False
    assert summary.folder_exists is True
    assert summary.in_database is True
    assert summary.title is None


def test_list_all_projects_empty_workspace_has_no_entries(
    conn: sqlite3.Connection, workspace: WorkspaceRoot
) -> None:
    assert list_all_projects(conn, workspace) == []


def test_list_all_projects_excludes_workspace_operation_scope(
    conn: sqlite3.Connection, workspace: WorkspaceRoot
) -> None:
    record_operation(
        conn,
        project_key=WORKSPACE_OPERATION_PROJECT,
        operation_type="create_compact",
    )
    assert list_all_projects(conn, workspace) == []


def test_delete_project_refuses_to_remove_live_knowledge(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rebuild_index(conn, workspace, [project])
    project_dir = workspace / "projects" / project
    assert project_dir.is_dir()

    with pytest.raises(ValidationError, match="hard-delete"):
        delete_project(conn, workspace, project)

    assert project_dir.is_dir()
    assert project in load_registry(workspace)


def test_delete_project_cleans_up_db_rows_when_folder_already_gone(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    """Mirrors the reported scenario: files deleted by hand, DB rows remain."""
    rebuild_index(conn, workspace, [project])
    import shutil

    shutil.rmtree(workspace / "projects" / project)

    result = delete_project(conn, workspace, project)

    assert result.folder_removed is False
    assert result.registry_removed is True
    assert result.rows_removed > 0
    assert list_all_projects(conn, workspace) == []


def test_delete_project_unknown_key_raises(
    conn: sqlite3.Connection, workspace: WorkspaceRoot
) -> None:
    with pytest.raises(ProjectNotFoundError):
        delete_project(conn, workspace, "nope")


def test_delete_project_rejects_traversal_without_touching_target(
    conn: sqlite3.Connection, workspace: WorkspaceRoot
) -> None:
    # The key used to be joined before validation, allowing ../../ traversal
    # into any directory reachable from the workspace.
    outside = workspace.parent / "must-survive"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(ValidationError):
        delete_project(conn, workspace, "../../must-survive")

    assert marker.read_text(encoding="utf-8") == "keep"
