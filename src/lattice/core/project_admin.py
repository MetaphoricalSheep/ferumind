"""Whole-project administration: cross-source listing and stale-state cleanup.

Project cleanup is not part of the
MCP surface (product/spec-mcp.md has no ``delete_project`` tool — see the
closed content-mutating list in AGENTS.md). It is an operator-driven
maintenance action, exposed only through the CLI, for reconciling stale
registry and database state after a project folder has already been removed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from lattice.core.errors import ProjectNotFoundError, ValidationError
from lattice.core.locks import acquire_workspace_lock
from lattice.core.operations import WORKSPACE_OPERATION_PROJECT
from lattice.core.paths import WorkspaceRoot, contained_path
from lattice.core.registry import load_registry, remove_registry_entry, validate_project_key
from lattice.core.types import DbConnection

#: Tables carrying a ``project_key`` column, cleaned up on project delete.
_PROJECT_KEY_TABLES: tuple[str, ...] = (
    "documents",
    "search_index",
    "operations",
    "snapshots",
    "mcp_call_observations",
)


class ProjectSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str | None
    status: str | None
    in_registry: bool
    folder_exists: bool
    in_database: bool


def list_all_projects(conn: DbConnection, workspace: WorkspaceRoot) -> list[ProjectSummary]:
    """Union of registry entries, on-disk project folders, and DB rows.

    Each project key is reported once even when it appears in more than one
    source, so a project half-cleaned-up (e.g. folder deleted by hand but
    rows still indexed) shows up as a single entry with the mismatch visible
    in its flags rather than as a duplicate row.
    """
    registry = load_registry(workspace)

    projects_root = contained_path(workspace, "projects")
    folder_keys: set[str] = (
        {entry.name for entry in projects_root.iterdir() if entry.is_dir()}
        if projects_root.is_dir()
        else set()
    )

    db_keys: set[str] = set()
    for table in _PROJECT_KEY_TABLES:
        # S608: table names come only from the closed constant above.
        rows = conn.execute(
            f"SELECT DISTINCT project_key FROM {table}"  # noqa: S608
        ).fetchall()
        db_keys.update(row[0] for row in rows if row[0])

    all_keys = set(registry) | folder_keys | db_keys
    all_keys.discard(WORKSPACE_OPERATION_PROJECT)
    return [
        ProjectSummary(
            key=key,
            title=registry[key].title if key in registry else None,
            status=registry[key].status if key in registry else None,
            in_registry=key in registry,
            folder_exists=key in folder_keys,
            in_database=key in db_keys,
        )
        for key in sorted(all_keys)
    ]


class DeleteProjectResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    registry_removed: bool
    folder_removed: bool
    rows_removed: int


def delete_project(conn: DbConnection, workspace: WorkspaceRoot, key: str) -> DeleteProjectResult:
    """Clean stale registry and DB state after a project folder was removed.

    Lattice never hard-deletes live knowledge content. A still-present project
    folder therefore fails closed. Raises ``ProjectNotFoundError`` if the key
    is unknown to every source.
    """
    project_key = str(validate_project_key(key))
    with acquire_workspace_lock(workspace):
        registry = load_registry(workspace)
        projects_root = contained_path(workspace, "projects")
        folder = contained_path(projects_root, project_key)
        folder_exists = folder.is_dir()
        if folder_exists:
            raise ValidationError(
                "Refusing to hard-delete a project folder containing user knowledge",
                details={
                    "project": project_key,
                    "required_action": (
                        "Archive or export the project, then remove its folder explicitly "
                        "before running registry/database cleanup."
                    ),
                },
            )

        rows_removed = 0
        for table in _PROJECT_KEY_TABLES:
            # S608: table names come only from the closed constant above; the
            # project key remains a bound SQL parameter.
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE project_key = ?",  # noqa: S608
                (project_key,),
            )
            rows_removed += max(cursor.rowcount, 0)

        if project_key not in registry and not folder_exists and rows_removed == 0:
            conn.rollback()
            raise ProjectNotFoundError(
                f"Project {project_key!r} not found in registry, disk, or database"
            )

        registry_removed = remove_registry_entry(workspace, project_key)

        conn.commit()
        return DeleteProjectResult(
            key=project_key,
            registry_removed=registry_removed,
            folder_removed=False,
            rows_removed=rows_removed,
        )
