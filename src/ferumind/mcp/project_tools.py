"""MCP project-administration tools: create a project, rebuild its index.

``create_project`` is the one workspace-level write on the MCP surface — it
takes no ``project`` argument, because it is what makes a project key valid in
the first place. ``rebuild_index`` regenerates derived search state for one
project or all of them; the index is derived, so it is safe to run anytime.

The operator-only half of project administration — listing and deleting
projects — is deliberately not here and not on the MCP surface at all; it lives
in :mod:`ferumind.core.project_admin`, reachable from the CLI.

The other content-mutating families have their own registrars beside the core
modules they call: ``apply_patch`` with its proposals in
:mod:`ferumind.mcp.propose_tools`, document creation and capture in
:mod:`ferumind.mcp.document_tools`, uploads in
:mod:`ferumind.mcp.upload_tools`, and the archive lifecycle with snapshot
restore in :mod:`ferumind.mcp.lifecycle_tools`.
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

from typing import Annotated

from mcp.types import CallToolResult
from pydantic import Field

from ferumind.core import project_writes
from ferumind.core.errors import FerumindError
from ferumind.core.indexer import rebuild_index
from ferumind.core.paths import PathSafetyError
from ferumind.core.registry import list_entries
from ferumind.mcp.models import FerumindResult, make_success, write_annotations
from ferumind.mcp.protocols import ToolRegistrar
from ferumind.mcp.result_models import CreateProjectData, RebuildIndexData
from ferumind.mcp.tool_context import (
    error_result,
    require_database,
    require_format_gate,
    require_workspace,
    scoped_project,
)


def register_project_tools(mcp: ToolRegistrar) -> None:
    """Register the project-administration tool family."""

    @mcp.tool(
        name="create_project",
        title="Create Project",
        description=(
            "Workspace-level: register a new project and seed its spine + folder skeleton "
            "from system/templates/. No project argument."
        ),
        annotations=write_annotations(),
    )
    def create_project_tool(
        key: Annotated[
            str,
            Field(
                description="Project key: lowercase letters, digits, hyphens; starts with a letter"
            ),
        ],
        title: Annotated[str, Field(description="Project title", min_length=1)],
    ) -> Annotated[CallToolResult, FerumindResult[CreateProjectData]]:
        try:
            require_format_gate().check_write()
            db = require_database()
            conn = db.get_connection()
            try:
                result = project_writes.create_project(
                    conn, require_workspace(), key=key, title=title
                )
            finally:
                conn.close()
            return make_success(
                {
                    "key": result.key,
                    "title": result.title,
                    "path": result.path,
                    "operation_id": result.operation_id,
                    "snapshot_id": result.snapshot_id,
                    "seeded": list(result.seeded),
                },
                project=result.key,
            )
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc)

    @mcp.tool(
        name="rebuild_index",
        title="Rebuild Index",
        description=(
            "Rebuild the derived search index from the Markdown on disk, for one project "
            "or all projects. Safe anytime: the index is derived state."
        ),
        annotations=write_annotations(),
    )
    def rebuild_index_tool(
        project: Annotated[
            str | None, Field(description="Project key, or omit to rebuild every project")
        ] = None,
    ) -> Annotated[CallToolResult, FerumindResult[RebuildIndexData]]:
        try:
            require_format_gate().check_write()
            ws = require_workspace()
            if project is not None:
                keys = [scoped_project(project).key]
            else:
                keys = [entry.key for entry in list_entries(ws)]
            db = require_database()
            conn = db.get_connection()
            try:
                result = rebuild_index(conn, ws, keys)
            finally:
                conn.close()
            return make_success(
                {
                    "projects": list(keys),
                    "documents_indexed": result.documents_indexed,
                    "documents_removed": result.documents_removed,
                    "errors": result.errors,
                    "error_messages": list(result.error_messages),
                },
                project=project,
            )
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)
