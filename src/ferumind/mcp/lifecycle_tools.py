"""MCP tools for the archive lifecycle and snapshot restore.

The three tools here move a document between ``archive/`` and its origin, or
put a snapshot's content back on disk. Every mutation is snapshot-protected and
operation-logged, and writes are refused with ``FORMAT_UNSUPPORTED`` on a
mismatched workspace format.

The other content-mutating families have their own registrars beside the core
modules they call: ``apply_patch`` with its proposals in
:mod:`ferumind.mcp.propose_tools`, document creation and capture in
:mod:`ferumind.mcp.document_tools`, uploads in
:mod:`ferumind.mcp.upload_tools`, and project administration in
:mod:`ferumind.mcp.project_tools`.
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

from typing import Annotated

from mcp.types import CallToolResult
from pydantic import Field

from ferumind.core import lifecycle_writes
from ferumind.core.errors import FerumindError
from ferumind.core.paths import PathSafetyError
from ferumind.mcp.models import (
    FerumindResult,
    make_success,
    write_annotations,
    write_result_data,
)
from ferumind.mcp.protocols import ToolRegistrar
from ferumind.mcp.result_models import ArchiveData, RestoreSnapshotData
from ferumind.mcp.tool_context import (
    error_result,
    require_database,
    require_format_gate,
    require_workspace,
    scoped_project,
)

_PROJECT_FIELD = Field(description="Project key; validated against the registry, never an override")


def register_lifecycle_tools(mcp: ToolRegistrar) -> None:
    """Register the archive-lifecycle and restore tool family."""

    @mcp.tool(
        name="archive_document",
        title="Archive Document",
        description=(
            "Archive a document: sets status: archived and moves it to "
            "archive/<original-path>. Snapshot-protected; refuses the spine. "
            "Archived documents vanish from get_context and default search."
        ),
        annotations=write_annotations(),
    )
    def archive_document_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, Field(description="Project-relative Markdown path")],
    ) -> Annotated[CallToolResult, FerumindResult[ArchiveData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = lifecycle_writes.archive_document(
                    conn, require_workspace(), entry.key, path=path
                )
            finally:
                conn.close()
            return make_success(
                {
                    "operation_id": result.operation_id,
                    "snapshot_id": result.snapshot_id,
                    "path": result.path,
                    "archived_path": result.archived_path,
                    "document_sha256": result.document_sha256,
                    "document_mutated": True,
                },
                project=entry.key,
            )
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="unarchive_document",
        title="Unarchive Document",
        description=(
            "Reverse an archive: move the document back to its mirror origin with "
            "status: active. Fails with PATH_EXISTS on a collision at the origin."
        ),
        annotations=write_annotations(),
    )
    def unarchive_document_tool(
        project: Annotated[str, _PROJECT_FIELD],
        archived_path: Annotated[
            str, Field(description="Path under archive/, e.g. archive/canvases/plan.md")
        ],
    ) -> Annotated[CallToolResult, FerumindResult[ArchiveData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = lifecycle_writes.unarchive_document(
                    conn, require_workspace(), entry.key, archived_path=archived_path
                )
            finally:
                conn.close()
            return make_success(
                {
                    "operation_id": result.operation_id,
                    "snapshot_id": result.snapshot_id,
                    "path": result.path,
                    "archived_path": result.archived_path,
                    "document_sha256": result.document_sha256,
                    "document_mutated": True,
                },
                project=entry.key,
            )
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="restore_snapshot",
        title="Restore Snapshot",
        description=(
            "Restore a file from a snapshot. A pre-restore snapshot is taken "
            "first, so the restore itself is reversible. Snapshot content must "
            "satisfy the current document contract; legacy snapshots remain "
            "readable but malformed current-format content is not written."
        ),
        annotations=write_annotations(),
    )
    def restore_snapshot_tool(
        project: Annotated[str, _PROJECT_FIELD],
        snapshot_id: Annotated[str, Field(description="Snapshot id from list_snapshots")],
    ) -> Annotated[CallToolResult, FerumindResult[RestoreSnapshotData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = lifecycle_writes.restore_snapshot(
                    conn, require_workspace(), entry.key, snapshot_id
                )
            finally:
                conn.close()
            data = write_result_data(result)
            data["restored_from_snapshot_id"] = result.restored_from_snapshot_id
            data["rollback_snapshot_id"] = result.rollback_snapshot_id
            data["document_mutated"] = True
            return make_success(data, project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)
