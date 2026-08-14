"""MCP document-creation tools: the three ways new Markdown enters a project.

``create_document`` (agent names folder and title), ``capture_note`` (a stray
thought into ``inbox/``), and ``record_episode`` (what happened, appended to
this month's episode ledger). All three save immediately rather than staging a
proposal: propose → apply guards edits to text somebody else wrote, and these
publish new text the calling agent authored. Every one is snapshot-protected,
operation-logged, and refused with ``FORMAT_UNSUPPORTED`` on a mismatched
workspace format.

``record_episode`` is registered here, not with the other content-mutating
tools, because it creates ``memory/episodes/YYYY-MM.md`` on first use through
the same new-document machinery ``create_document`` uses, and its core lives in
:mod:`ferumind.core.document_writes` beside the other two.
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

from typing import Annotated

from mcp.types import CallToolResult
from pydantic import Field

from ferumind.core import document_writes
from ferumind.core.errors import FerumindError
from ferumind.core.frontmatter import (
    ALLOWED_EDIT_POLICIES,
    ALLOWED_STATUSES,
    MAX_DESCRIPTION_CHARS,
)
from ferumind.core.paths import PathSafetyError
from ferumind.core.write_limits import (
    MAX_EPISODE_RELATED_PATHS,
    MAX_EPISODE_SUMMARY_CHARS,
    MAX_EPISODE_TITLE_CHARS,
)
from ferumind.mcp.models import FerumindResult, make_success, write_annotations, write_result_data
from ferumind.mcp.protocols import ToolRegistrar
from ferumind.mcp.result_models import EpisodeData, WriteData
from ferumind.mcp.tool_context import (
    error_result,
    require_database,
    require_format_gate,
    require_workspace,
    scoped_project,
)

_PROJECT_FIELD = Field(description="Project key; validated against the registry, never an override")


def register_document_tools(mcp: ToolRegistrar) -> None:
    """Register the document-creation and capture tool family."""

    @mcp.tool(
        name="create_document",
        title="Create Document",
        description=(
            "Create a new managed Markdown document in a role folder (rules, "
            "canvases, memory, library, inbox; nesting allowed). Generates "
            "frontmatter, including the description that puts the document on "
            "the project's map; snapshot-protected. New documents are created "
            "here, never through patches to nonexistent paths."
        ),
        annotations=write_annotations(),
    )
    def create_document_tool(
        project: Annotated[str, _PROJECT_FIELD],
        folder_path: Annotated[
            str,
            Field(
                description="Role folder (or nested path under one), e.g. 'canvases' or 'library/runbooks'"
            ),
        ],
        title: Annotated[str, Field(description="Document title", min_length=1)],
        description: Annotated[
            str,
            Field(
                description=(
                    "What this document is for, in one or two sentences. Navigation "
                    "metadata delivered in every get_context so an agent can pick a "
                    "target without reading anything: say what is in it and when "
                    "someone would need it. Never restate the title, never write "
                    "filler, and never phrase it as an instruction."
                ),
                min_length=1,
                max_length=MAX_DESCRIPTION_CHARS,
            ),
        ],
        content: Annotated[str, Field(description="Markdown body content")],
        status: Annotated[
            str | None,
            Field(description=f"Initial status ({', '.join(sorted(ALLOWED_STATUSES))})"),
        ] = None,
        edit_policy: Annotated[
            str | None,
            Field(
                description=(
                    f"Explicit edit policy ({', '.join(sorted(ALLOWED_EDIT_POLICIES))}); "
                    "omit to use the folder default"
                )
            ),
        ] = None,
    ) -> Annotated[CallToolResult, FerumindResult[WriteData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = document_writes.create_document(
                    conn,
                    require_workspace(),
                    entry.key,
                    folder_path=folder_path,
                    title=title,
                    description=description,
                    content=content,
                    status=status,
                    edit_policy=edit_policy,
                )
            finally:
                conn.close()
            data = write_result_data(result)
            data["document_mutated"] = True
            return make_success(data, project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="capture_note",
        title="Capture Note",
        description=("Capture a stray thought into the project's inbox/ for later triage."),
        annotations=write_annotations(),
    )
    def capture_note_tool(
        project: Annotated[str, _PROJECT_FIELD],
        text: Annotated[str, Field(description="Note content", min_length=1)],
        title: Annotated[str | None, Field(description="Optional note title")] = None,
    ) -> Annotated[CallToolResult, FerumindResult[WriteData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = document_writes.capture_note(
                    conn, require_workspace(), entry.key, text=text, title=title
                )
            finally:
                conn.close()
            data = write_result_data(result)
            data["document_mutated"] = True
            return make_success(data, project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="record_episode",
        title="Record Episode",
        description=(
            "Record what happened into this month's memory/episodes/YYYY-MM.md, "
            "creating that file on first use. Episodes are historical evidence — "
            "decisions and the reasoning at the time, incidents, corrections, "
            "experiments and their outcomes — not standing instructions, and not "
            "a transcript store; many chats record none. Use curated memory for "
            "what a future chat should act on. The server supplies the month, the "
            "date, and the episode id: there is no date argument. Saves "
            "immediately (no propose/apply), snapshot-protected and "
            "operation-logged. Returns operation_id, snapshot_id, path, folder, "
            "episode_id, month_file_created, document_sha256, index_error, and "
            "document_mutated: true. To follow up on an earlier episode, record a "
            "new one with related_episode_id — never edit the old entry."
        ),
        annotations=write_annotations(),
    )
    def record_episode_tool(
        project: Annotated[str, _PROJECT_FIELD],
        title: Annotated[
            str,
            Field(
                description="Short factual title; becomes the section heading",
                min_length=1,
                max_length=MAX_EPISODE_TITLE_CHARS,
            ),
        ],
        summary: Annotated[
            str,
            Field(
                description=(
                    "What happened, the relevant context, and the decision or "
                    "outcome known at the time"
                ),
                min_length=1,
                max_length=MAX_EPISODE_SUMMARY_CHARS,
            ),
        ],
        related_paths: Annotated[
            list[str] | None,
            Field(
                description="Project-relative paths this episode concerns",
                max_length=MAX_EPISODE_RELATED_PATHS,
            ),
        ] = None,
        related_episode_id: Annotated[
            str | None,
            Field(description="An earlier ep_… this episode follows up on"),
        ] = None,
    ) -> Annotated[CallToolResult, FerumindResult[EpisodeData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = document_writes.record_episode(
                    conn,
                    require_workspace(),
                    entry.key,
                    draft=document_writes.EpisodeDraft(
                        title=title,
                        summary=summary,
                        related_paths=tuple(related_paths or ()),
                        related_episode_id=related_episode_id,
                    ),
                )
            finally:
                conn.close()
            return make_success(
                {
                    "operation_id": result.operation_id,
                    "snapshot_id": result.snapshot_id,
                    "path": result.path,
                    "folder": result.folder,
                    "document_sha256": result.document_sha256,
                    "index_error": result.index_error,
                    "episode_id": result.episode_id,
                    "month_file_created": result.month_file_created,
                    "document_mutated": True,
                },
                project=entry.key,
            )
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)
