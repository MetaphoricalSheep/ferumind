"""MCP tools for workspace-level compacts."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, cast

from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from ferumind.core import compacts
from ferumind.core.errors import FerumindError, ValidationError
from ferumind.core.paths import PathSafetyError
from ferumind.core.registry import ProjectEntry
from ferumind.core.types import JsonObject, JsonValue
from ferumind.mcp.models import make_success, read_only_annotations, write_annotations
from ferumind.mcp.protocols import ToolRegistrar
from ferumind.mcp.tool_context import (
    error_result,
    require_database,
    require_format_gate,
    require_workspace,
    scoped_project,
)

type FerumindToolResult = CallToolResult


def _dump_model(model: BaseModel) -> JsonObject:
    return cast(JsonObject, model.model_dump(mode="json"))


def _dump_models(models: Sequence[BaseModel]) -> list[JsonValue]:
    return [cast(JsonValue, _dump_model(model)) for model in models]


def _validate_optional_project(project: str | None) -> ProjectEntry | None:
    if project is None:
        return None
    if not project.strip():
        return None
    return scoped_project(project)


def _coerce_list(values: list[str] | None) -> list[str]:
    return [] if values is None else values


def register_compact_tools(mcp: ToolRegistrar) -> None:
    """Register workspace-level compact tools."""

    @mcp.tool(
        name="get_compact_instructions",
        title="Get Compact Instructions",
        description=(
            "Return the procedure for creating a workspace-level Ferumind compact. "
            "Use only when the user explicitly invokes `/compact`, `@ferumind /compact`, "
            "or names a Ferumind compact. Do not use for ordinary project memory, "
            "notes, summaries, or document updates."
        ),
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def get_compact_instructions_tool() -> FerumindToolResult:
        try:
            require_format_gate().check_read()
            return make_success({"instructions": compacts.compact_instructions()})
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc)

    @mcp.tool(
        name="create_compact_draft",
        title="Create Compact Draft",
        description=(
            "Create a workspace-level compact draft under workspace/compacts/. "
            "Use after get_compact_instructions when chunking a chat compact."
        ),
        annotations=write_annotations(),
        structured_output=False,
    )
    def create_compact_draft_tool(
        project: Annotated[
            str | None,
            Field(description="Optional project key for metadata only; it does not scope the path"),
        ] = None,
        sources: Annotated[
            list[str] | None,
            Field(description="Source refs such as document paths, URLs, or user-visible labels"),
        ] = None,
        tags: Annotated[list[str] | None, Field(description="Optional compact tags")] = None,
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            project_entry = _validate_optional_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = compacts.create_compact_draft(
                    conn,
                    require_workspace(),
                    project=project_entry,
                    sources=_coerce_list(sources),
                    tags=_coerce_list(tags),
                )
            finally:
                conn.close()
            return make_success(_dump_model(result))
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="append_compact_chunk",
        title="Append Compact Chunk",
        description=(
            "Append an agent-produced chunk summary to a compact draft. The server "
            "stores the chunk; the chat agent remains responsible for summarizing."
        ),
        annotations=write_annotations(),
        structured_output=False,
    )
    def append_compact_chunk_tool(
        token: Annotated[str, Field(description="Four-word compact token")],
        chunk_markdown: Annotated[str, Field(description="Agent-produced chunk summary")],
        sources: Annotated[
            list[str] | None,
            Field(description="Additional source refs covered by this chunk"),
        ] = None,
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            db = require_database()
            conn = db.get_connection()
            try:
                result = compacts.append_compact_chunk(
                    conn,
                    require_workspace(),
                    token=token,
                    chunk_markdown=chunk_markdown,
                    sources=_coerce_list(sources),
                )
            finally:
                conn.close()
            return make_success(_dump_model(result))
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc)

    @mcp.tool(
        name="finalize_compact",
        title="Finalize Compact",
        description=(
            "Finalize a compact draft with the complete Markdown body. The body "
            "must begin with the Handoff Prompt block containing handoff_prompt."
        ),
        annotations=write_annotations(),
        structured_output=False,
    )
    def finalize_compact_tool(
        token: Annotated[str, Field(description="Four-word compact token")],
        handoff_prompt: Annotated[str, Field(description="Mandatory resume handoff prompt")],
        final_markdown: Annotated[
            str,
            Field(description="Final compact body, beginning with the Handoff Prompt block"),
        ],
        sources: Annotated[list[str] | None, Field(description="Final source refs")] = None,
        tags: Annotated[list[str] | None, Field(description="Final compact tags")] = None,
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            db = require_database()
            conn = db.get_connection()
            try:
                result = compacts.finalize_compact(
                    conn,
                    require_workspace(),
                    token=token,
                    handoff_prompt=handoff_prompt,
                    final_markdown=final_markdown,
                    sources=_coerce_list(sources),
                    tags=_coerce_list(tags),
                )
            finally:
                conn.close()
            return make_success(_dump_model(result))
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc)

    @mcp.tool(
        name="read_compact",
        title="Read Compact",
        description="Read a workspace-level compact by its four-word token.",
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def read_compact_tool(
        token: Annotated[str, Field(description="Four-word compact token")],
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_read()
            result = compacts.read_compact(require_workspace(), token=token)
            return make_success(_dump_model(result))
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc)

    @mcp.tool(
        name="resume_compact",
        title="Resume Compact",
        description=(
            "Resume a workspace-level compact by token. Verifies integrity, "
            "increments resume_count, and returns the handoff prompt and body."
        ),
        annotations=write_annotations(),
        structured_output=False,
    )
    def resume_compact_tool(
        token: Annotated[str, Field(description="Four-word compact token")],
        auto_archive_on_resume: Annotated[
            bool,
            Field(description="Set compact state to archived immediately after resume"),
        ] = False,
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            db = require_database()
            conn = db.get_connection()
            try:
                result = compacts.resume_compact(
                    conn,
                    require_workspace(),
                    token=token,
                    auto_archive_on_resume=auto_archive_on_resume,
                )
            finally:
                conn.close()
            return make_success(_dump_model(result))
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc)

    @mcp.tool(
        name="list_compacts",
        title="List Compacts",
        description="List workspace-level compact metadata; bodies are not included.",
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def list_compacts_tool(
        state: Annotated[str | None, Field(description="Optional compact state filter")] = None,
        project: Annotated[
            str | None,
            Field(description="Optional project metadata filter; validates if supplied"),
        ] = None,
        limit: Annotated[
            int,
            Field(
                description="Maximum compact rows to return",
                ge=1,
                le=compacts.MAX_COMPACT_LIST_LIMIT,
            ),
        ] = 50,
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_read()
            project_entry = _validate_optional_project(project)
            project_filter = project_entry.key if project_entry is not None else None
            result = compacts.list_compacts(
                require_workspace(),
                state=state,
                project=project_filter,
                limit=limit,
            )
            return make_success({"compacts": _dump_models(result)})
        except (FerumindError, PathSafetyError, ValidationError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="archive_compact",
        title="Archive Compact",
        description="Archive a workspace-level compact by setting state: archived. No hard delete.",
        annotations=write_annotations(),
        structured_output=False,
    )
    def archive_compact_tool(
        token: Annotated[str, Field(description="Four-word compact token")],
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            db = require_database()
            conn = db.get_connection()
            try:
                result = compacts.archive_compact(conn, require_workspace(), token=token)
            finally:
                conn.close()
            return make_success(_dump_model(result))
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc)
