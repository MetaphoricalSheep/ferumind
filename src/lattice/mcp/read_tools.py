"""MCP read-only tools (spec-mcp §5.1).

Every project-scoped tool takes a required ``project`` argument validated
against the registry. Reads reconcile out-of-band drift before serving
content (00 D12) and stay available on an older-format workspace.
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

from typing import Annotated, Literal, cast

from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from lattice.core import search as search_core
from lattice.core.context import build_context
from lattice.core.document_map import (
    build_document_map,
    read_document_range,
)
from lattice.core.edit_targets import find_in_document
from lattice.core.errors import LatticeError
from lattice.core.folders import ROLE_FOLDERS
from lattice.core.locks import acquire_project_lock
from lattice.core.operations import list_operations, list_pending_proposals
from lattice.core.paths import PathSafetyError, contained_project_root
from lattice.core.reads import (
    list_project_tree,
    read_project_document,
    read_project_snapshot,
)
from lattice.core.reconcile import reconcile_document, reconcile_project
from lattice.core.registry import list_entries
from lattice.core.snapshots import list_snapshots_from_db
from lattice.core.types import JsonObject, JsonValue
from lattice.mcp.models import make_success, read_only_annotations
from lattice.mcp.protocols import ToolRegistrar
from lattice.mcp.tool_context import (
    error_result,
    require_database,
    require_format_gate,
    require_workspace,
    scoped_project,
)

type LatticeToolResult = CallToolResult

_PROJECT_FIELD = Field(description="Project key; validated against the registry, never an override")


def dump_model(model: BaseModel) -> JsonObject:
    """Serialize a pydantic model into an envelope-compatible JSON object."""
    return cast(JsonObject, model.model_dump(mode="json"))


def register_read_tools(mcp: ToolRegistrar) -> None:
    """Register the read-only tool family."""

    @mcp.tool(
        name="get_context",
        title="Get Context",
        description=(
            "The contract call — the first call of every chat. Returns the merged "
            "workspace + project rules, the spine, the document map, and the inbox "
            "count for the project, with payload-size telemetry."
        ),
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def get_context_tool(
        project: Annotated[str, _PROJECT_FIELD],
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_read()
            entry = scoped_project(project)
            project_root = contained_project_root(require_workspace(), entry.key)
            db = require_database()
            with acquire_project_lock(project_root, entry.key):
                conn = db.get_connection()
                try:
                    context = build_context(conn, require_workspace(), entry)
                finally:
                    conn.close()
            return make_success(dump_model(context), project=entry.key)
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="read_document",
        title="Read Document",
        description=(
            "Read a Markdown document from the project (any folder, including "
            "rules/, memory/, and archive/). Returns content, frontmatter, and "
            "document_sha256 for hash-guarded edits."
        ),
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def read_document_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, Field(description="Project-relative Markdown path")],
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_read()
            entry = scoped_project(project)
            project_root = contained_project_root(require_workspace(), entry.key)
            db = require_database()
            with acquire_project_lock(project_root, entry.key):
                conn = db.get_connection()
                try:
                    reconcile_document(conn, require_workspace(), entry.key, path)
                    document = read_project_document(
                        require_workspace(),
                        entry.key,
                        path,
                    )
                finally:
                    conn.close()
            return make_success(
                {
                    "path": document.path,
                    "folder": document.parsed.folder,
                    "status": document.parsed.status,
                    "edit_policy": document.parsed.edit_policy,
                    "title": document.parsed.title,
                    "content": document.content,
                    "frontmatter": document.parsed.frontmatter,
                    "document_sha256": document.parsed.sha256,
                },
                project=entry.key,
            )
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="read_document_range",
        title="Read Document Range",
        description=(
            "Read exact lines start_line..end_line of a document with a guarding "
            "range hash for subsequent hash-guarded edits."
        ),
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def read_document_range_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, Field(description="Project-relative Markdown path")],
        start_line: Annotated[int, Field(description="First line (1-indexed)", ge=1)],
        end_line: Annotated[int, Field(description="Last line (inclusive)", ge=1)],
        include_line_numbers: Annotated[
            bool, Field(description="Include a numbered rendering of the range")
        ] = True,
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_read()
            entry = scoped_project(project)
            project_root = contained_project_root(require_workspace(), entry.key)
            db = require_database()
            with acquire_project_lock(project_root, entry.key):
                conn = db.get_connection()
                try:
                    reconcile_document(conn, require_workspace(), entry.key, path)
                    document = read_project_document(
                        require_workspace(),
                        entry.key,
                        path,
                    )
                finally:
                    conn.close()
            result = read_document_range(
                content=document.content,
                project_key=entry.key,
                path=document.path,
                start_line=start_line,
                end_line=end_line,
                include_line_numbers=include_line_numbers,
            )
            return make_success(dump_model(result), project=entry.key)
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="get_document_map",
        title="Get Document Map",
        description=(
            "Structured map of a document: sections, blocks, and line ranges, each "
            "with a content hash — the lookup step before a targeted patch."
        ),
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def get_document_map_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, Field(description="Project-relative Markdown path")],
        include_blocks: Annotated[bool, Field(description="Include structural blocks")] = True,
        include_lines: Annotated[bool, Field(description="Include per-line hashes")] = False,
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_read()
            entry = scoped_project(project)
            project_root = contained_project_root(require_workspace(), entry.key)
            db = require_database()
            with acquire_project_lock(project_root, entry.key):
                conn = db.get_connection()
                try:
                    reconcile_document(conn, require_workspace(), entry.key, path)
                    document = read_project_document(
                        require_workspace(),
                        entry.key,
                        path,
                    )
                finally:
                    conn.close()
            document_map = build_document_map(
                content=document.content,
                project_key=entry.key,
                path=document.path,
                include_blocks=include_blocks,
                include_lines=include_lines,
            )
            return make_success(dump_model(document_map), project=entry.key)
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="find_in_document",
        title="Find In Document",
        description=(
            "Find literal or regex matches inside a single document with context "
            "lines and per-line hashes for anchoring edits."
        ),
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def find_in_document_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, Field(description="Project-relative Markdown path")],
        query: Annotated[str, Field(description="Text or regex to find", min_length=1)],
        mode: Annotated[Literal["literal", "regex"], Field(description="Match mode")] = "literal",
        case_sensitive: Annotated[bool, Field(description="Case-sensitive matching")] = False,
        include_context_lines: Annotated[
            int, Field(description="Context lines around each match", ge=0, le=10)
        ] = 2,
        include_code_blocks: Annotated[
            bool, Field(description="Match inside fenced code blocks")
        ] = True,
        limit: Annotated[int, Field(description="Maximum matches returned", ge=1, le=100)] = 20,
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_read()
            entry = scoped_project(project)
            project_root = contained_project_root(require_workspace(), entry.key)
            db = require_database()
            with acquire_project_lock(project_root, entry.key):
                conn = db.get_connection()
                try:
                    reconcile_document(conn, require_workspace(), entry.key, path)
                    document = read_project_document(
                        require_workspace(),
                        entry.key,
                        path,
                    )
                finally:
                    conn.close()
            result = find_in_document(
                content=document.content,
                project_key=entry.key,
                path=document.path,
                query=query,
                mode=mode,
                case_sensitive=case_sensitive,
                include_context_lines=include_context_lines,
                include_code_blocks=include_code_blocks,
                limit=limit,
            )
            return make_success(dump_model(result), project=entry.key)
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="search_project",
        title="Search Project",
        description=(
            "Full-text search (FTS5, bm25-ranked) over this project's indexed "
            "Project Markdown only — not external systems or other projects. Archived documents "
            "are excluded unless include_archived is set."
        ),
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def search_project_tool(
        project: Annotated[str, _PROJECT_FIELD],
        query: Annotated[str, Field(description="Search query", min_length=1)],
        folder: Annotated[
            str | None, Field(description=f"Restrict to a role folder ({', '.join(ROLE_FOLDERS)})")
        ] = None,
        status: Annotated[str | None, Field(description="Restrict to a document status")] = None,
        include_archived: Annotated[
            bool, Field(description="Include archived documents in results")
        ] = False,
        limit: Annotated[int, Field(description="Maximum results", ge=1, le=100)] = 20,
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_read()
            entry = scoped_project(project)
            project_root = contained_project_root(require_workspace(), entry.key)
            db = require_database()
            with acquire_project_lock(project_root, entry.key):
                conn = db.get_connection()
                try:
                    reconcile_project(conn, require_workspace(), entry.key)
                    results = search_core.search_project(
                        conn,
                        entry.key,
                        query,
                        folder=folder,
                        status=status,
                        include_archived=include_archived,
                        limit=limit,
                    )
                finally:
                    conn.close()
            return make_success(
                {
                    "query": query,
                    "results": cast("list[JsonValue]", [dump_model(r) for r in results]),
                    "count": len(results),
                },
                project=entry.key,
            )
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="list_tree",
        title="List Tree",
        description="List the project's Markdown files with folder, size, and status.",
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def list_tree_tool(
        project: Annotated[str, _PROJECT_FIELD],
        folder: Annotated[str | None, Field(description="Restrict to one role folder")] = None,
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_read()
            entry = scoped_project(project)
            project_root = contained_project_root(require_workspace(), entry.key)
            db = require_database()
            with acquire_project_lock(project_root, entry.key):
                conn = db.get_connection()
                try:
                    reconcile_project(conn, require_workspace(), entry.key)
                    tree_entries = list_project_tree(conn, entry.key, folder=folder)
                finally:
                    conn.close()
            tree = cast("list[JsonValue]", [dump_model(item) for item in tree_entries])
            return make_success({"tree": tree, "count": len(tree)}, project=entry.key)
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="list_pending_patches",
        title="List Pending Patches",
        description=(
            "List this project's pending patch proposals (id, path, age, "
            "expires_at). Expired proposals are swept first."
        ),
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def list_pending_patches_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str | None, Field(description="Filter by target path")] = None,
        limit: Annotated[int, Field(description="Maximum results", ge=1, le=100)] = 50,
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_read()
            entry = scoped_project(project)
            project_root = contained_project_root(require_workspace(), entry.key)
            db = require_database()
            with acquire_project_lock(project_root, entry.key):
                conn = db.get_connection()
                try:
                    pending = list_pending_proposals(
                        conn,
                        entry.key,
                        target_path=path,
                        limit=limit,
                    )
                finally:
                    conn.close()
            items: list[JsonValue] = [
                {
                    "operation_id": op.id,
                    "operation_type": op.operation_type,
                    "path": op.target_path,
                    "created_at": op.created_at,
                    "expires_at": op.expires_at,
                    "base_sha256": op.base_sha256,
                }
                for op in pending
            ]
            return make_success({"pending": items, "count": len(items)}, project=entry.key)
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="operation_log",
        title="Operation Log",
        description=(
            "Recent operations for this project (newest first), including "
            "out-of-band edits detected on disk (source: out-of-band)."
        ),
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def operation_log_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str | None, Field(description="Filter by target path")] = None,
        limit: Annotated[int, Field(description="Maximum results", ge=1, le=200)] = 50,
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_read()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                ops = list_operations(conn, entry.key, target_path=path, limit=limit)
            finally:
                conn.close()
            items: list[JsonValue] = [
                {
                    "operation_id": op.id,
                    "operation_type": op.operation_type,
                    "path": op.target_path,
                    "source": op.source,
                    "state": op.state,
                    "created_at": op.created_at,
                    "snapshot_id": op.snapshot_id,
                }
                for op in ops
            ]
            return make_success({"operations": items, "count": len(items)}, project=entry.key)
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="list_snapshots",
        title="List Snapshots",
        description="List snapshots for this project (optionally for one path), newest first.",
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def list_snapshots_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str | None, Field(description="Filter by target path")] = None,
        limit: Annotated[int, Field(description="Maximum results", ge=1, le=200)] = 50,
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_read()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                snapshots = list_snapshots_from_db(
                    conn, project_key=entry.key, target_path=path, limit=limit
                )
            finally:
                conn.close()
            items: list[JsonValue] = [
                {
                    "id": snapshot.id,
                    "project_key": snapshot.project_key,
                    "target_path": snapshot.target_path,
                    "reason": snapshot.reason,
                    "created_at": snapshot.created_at,
                }
                for snapshot in snapshots
            ]
            return make_success({"snapshots": items, "count": len(items)}, project=entry.key)
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="read_snapshot",
        title="Read Snapshot",
        description="Read a snapshot's metadata, before/after content, and diff.",
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def read_snapshot_tool(
        project: Annotated[str, _PROJECT_FIELD],
        snapshot_id: Annotated[str, Field(description="Snapshot id from list_snapshots")],
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_read()
            entry = scoped_project(project)
            ws = require_workspace()
            project_dir = contained_project_root(ws, entry.key)
            with acquire_project_lock(project_dir, entry.key):
                snapshot = read_project_snapshot(ws, entry.key, snapshot_id)
            return make_success(dump_model(snapshot), project=entry.key)
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="list_projects",
        title="List Projects",
        description="List registered projects (key, title, status). Workspace-level; no project argument.",
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def list_projects_tool() -> LatticeToolResult:
        try:
            require_format_gate().check_read()
            entries = list_entries(require_workspace())
            items: list[JsonValue] = [
                {"key": e.key, "title": e.title, "status": e.status} for e in entries
            ]
            return make_success({"projects": items, "count": len(items)})
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc)
