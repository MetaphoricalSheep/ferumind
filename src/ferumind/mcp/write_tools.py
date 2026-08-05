"""MCP content-mutating tools (spec-mcp §5.3).

Every mutation is snapshot-protected and operation-logged; only an
``apply_patch`` result with ``document_mutated=true`` means a file was
saved. Writes are refused with ``FORMAT_UNSUPPORTED`` on a mismatched
workspace format.
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

from typing import Annotated, Any, Final, cast

import anyio
from anyio.to_thread import run_sync
from mcp.types import CallToolResult
from pydantic import Field

from ferumind.core import writes
from ferumind.core.errors import FerumindError
from ferumind.core.frontmatter import ALLOWED_EDIT_POLICIES, ALLOWED_STATUSES
from ferumind.core.indexer import rebuild_index
from ferumind.core.paths import PathSafetyError
from ferumind.core.registry import list_entries
from ferumind.core.types import JsonObject
from ferumind.mcp.models import (
    apply_state_fields,
    make_success,
    open_world_write_annotations,
    proposal_annotations,
    write_annotations,
)
from ferumind.mcp.protocols import ToolRegistrar
from ferumind.mcp.tool_context import (
    error_result,
    require_config,
    require_database,
    require_format_gate,
    require_workspace,
    scoped_project,
)

type FerumindToolResult = CallToolResult

_PROJECT_FIELD = Field(description="Project key; validated against the registry, never an override")
_MAX_BASE64_CHUNK_CHARS = ((writes.MAX_CHUNK_BYTES + 2) // 3) * 4

#: ChatGPT's own file-reference schema (spec-mcp §5.3c) — properties/
#: required/additionalProperties must match this exactly for ChatGPT's
#: openai/fileParams extension to recognize it.
_CHATGPT_FILE_ITEM_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "download_url": {"type": "string"},
        "file_id": {"type": "string"},
        "mime_type": {"type": "string"},
        "file_name": {"type": "string"},
    },
    "required": ["download_url", "file_id"],
    "additionalProperties": False,
}


def _pin_chatgpt_file_schema(mcp: ToolRegistrar) -> None:
    """Force the exact ChatGPT file-reference schema onto both upload tools.

    FastMCP derives the ``upload_*_from_chatgpt`` inputSchemas from
    :class:`writes.ChatGPTFileInput` automatically, which represents the
    optional ``mime_type``/``file_name`` fields as ``anyOf: [string, null]``
    (idiomatic pydantic/JSON-Schema) rather than the plain
    ``{"type": "string"}`` ChatGPT's own file-reference schema uses for
    every property. Runtime argument validation still goes through the
    pydantic model — a strict superset, since it also accepts an explicit
    ``null`` — so only the *advertised* ``tools/list`` schema needs
    normalizing here. FastMCP exposes no public API to edit a registered
    tool's schema (same rationale as
    ``observation.apply_observation_to_all_tools`` reaching into
    ``_tool_manager`` directly).

    The batch tool declares an array of file references; the single-file
    tool declares one file reference directly (the same object schema, not
    wrapped in an array) — both must be top level, since the extension does
    not resolve nested file params.
    """
    tool_manager = cast(Any, mcp)._tool_manager  # pyright: ignore[reportPrivateUsage]
    tools = tool_manager._tools  # pyright: ignore[reportPrivateUsage]

    batch = tools.get("upload_library_files_from_chatgpt")
    if batch is not None:
        files_schema = batch.parameters.get("properties", {}).get("files")
        if isinstance(files_schema, dict):
            files_schema["items"] = _CHATGPT_FILE_ITEM_SCHEMA
        batch.parameters.get("$defs", {}).pop("ChatGPTFileInput", None)

    single = tools.get("upload_library_file_from_chatgpt")
    if single is not None:
        properties = single.parameters.get("properties", {})
        file_schema = properties.get("file")
        if isinstance(file_schema, dict):
            # Replace in place: the description FastMCP attached to the
            # parameter is dropped along with the $ref, matching the exact
            # item schema ChatGPT looks for.
            properties["file"] = dict(_CHATGPT_FILE_ITEM_SCHEMA)
        single.parameters.get("$defs", {}).pop("ChatGPTFileInput", None)


def _write_result_data(result: writes.WriteResult) -> JsonObject:
    return {
        "operation_id": result.operation_id,
        "snapshot_id": result.snapshot_id,
        "path": result.path,
        "folder": result.folder,
        "document_sha256": result.document_sha256,
        "index_error": result.index_error,
    }


def register_write_tools(mcp: ToolRegistrar) -> None:
    """Register the content-mutating tool family."""

    remote_download_limiter = anyio.CapacityLimiter(2)

    @mcp.tool(
        name="apply_patch",
        title="Apply Patch",
        description=(
            "Apply a previously proposed patch by operation_id. Revalidates the "
            "proposal binding, hash guards, and 24 h TTL; snapshots before "
            "writing. Returns the new document_sha256 — chain it into the next "
            "edit's expected_document_sha256 instead of re-reading."
        ),
        annotations=write_annotations(),
        structured_output=False,
    )
    def apply_patch_tool(
        project: Annotated[str, _PROJECT_FIELD],
        operation_id: Annotated[str, Field(description="Operation id from a propose_* tool")],
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.apply_patch(conn, require_workspace(), entry.key, operation_id)
            finally:
                conn.close()
            data = _write_result_data(result)
            data["diff"] = result.diff
            data.update(apply_state_fields(operation_id, result.operation_id))
            return make_success(data, project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="create_document",
        title="Create Document",
        description=(
            "Create a new managed Markdown document in a role folder (rules, "
            "canvases, memory, library, inbox; nesting allowed). Generates "
            "frontmatter; snapshot-protected. New documents are created here, "
            "never through patches to nonexistent paths."
        ),
        annotations=write_annotations(),
        structured_output=False,
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
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.create_document(
                    conn,
                    require_workspace(),
                    entry.key,
                    folder_path=folder_path,
                    title=title,
                    content=content,
                    status=status,
                    edit_policy=edit_policy,
                )
            finally:
                conn.close()
            data = _write_result_data(result)
            data["document_mutated"] = True
            return make_success(data, project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="upload_library_file",
        title="Upload Library File",
        description=(
            "Upload a binary/non-Markdown file (PDF, image, spreadsheet, etc.) "
            "into library/. Always lands under library/; folder_path may nest "
            "below it, just like create_document (e.g. 'library/attachments'). "
            "Provide the real filename with its extension. content_base64 is "
            "the raw file bytes, base64-encoded — decoded size is capped at "
            "256 KB (this whole call has to fit in one tool call; the base64 "
            "string itself runs ~33% larger on the wire). For anything bigger, "
            "use start_library_file_upload/append_upload_chunk/"
            "finalize_library_file_upload instead. Scripts and executables are "
            "blocked by extension. Fails closed if a file already exists at "
            "that path — no silent overwrite; choose a different filename or "
            "folder instead. A "
            "sidecar '<stem>.json' (the filename with its extension replaced by "
            ".json) is written next to the file: the server stamps a few audit "
            "fields (sha256, size_bytes, "
            "uploaded_at, mime_type), and any additional keys passed in "
            "metadata are kept as-is — that content is yours to shape. Not "
            "yet indexed by search_project or list_tree; keep the returned "
            "path if you need to reference the file again."
            "Images (JPEG/PNG/WebP) are normalized for storage before they are "
            "written: downscaled to the workspace's configured longest edge and "
            "re-encoded, so the stored file is usually much smaller than what you "
            "sent. The full-resolution original is NOT retained. A lossless source "
            "(PNG, lossless WebP) stays lossless and pixel-identical, and nothing "
            "is ever made larger. Because of this the returned sha256 and "
            "size_bytes describe the STORED bytes, not the bytes you uploaded — "
            "do not treat a difference as a transfer error."
        ),
        annotations=write_annotations(),
        structured_output=False,
    )
    def upload_library_file_tool(
        project: Annotated[str, _PROJECT_FIELD],
        filename: Annotated[
            str,
            Field(
                description="Real filename with extension, e.g. 'q3-report.pdf'",
                min_length=1,
                max_length=writes.MAX_UPLOAD_FILENAME_BYTES,
            ),
        ],
        content_base64: Annotated[
            str,
            Field(description="Raw file bytes, base64-encoded", max_length=_MAX_BASE64_CHUNK_CHARS),
        ],
        folder_path: Annotated[
            str,
            Field(description="Must be under library/, may nest, e.g. 'library/attachments'"),
        ] = "library",
        mime_type: Annotated[
            str | None,
            Field(
                description="MIME type of the file, if known", max_length=writes.MAX_MIME_TYPE_CHARS
            ),
        ] = None,
        metadata: Annotated[
            JsonObject | None,
            Field(description="Freeform metadata to store in the sidecar JSON; agent-authored"),
        ] = None,
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.upload_library_file(
                    conn,
                    require_workspace(),
                    entry.key,
                    filename=filename,
                    content_base64=content_base64,
                    folder_path=folder_path,
                    mime_type=mime_type,
                    metadata=metadata,
                    image_policy=require_config().image_policy,
                )
            finally:
                conn.close()
            return make_success(
                {
                    "operation_id": result.operation_id,
                    "snapshot_id": result.snapshot_id,
                    "path": result.path,
                    "metadata_path": result.metadata_path,
                    "folder": result.folder,
                    "sha256": result.sha256,
                    "size_bytes": result.size_bytes,
                    "mime_type": result.mime_type,
                    "document_mutated": True,
                },
                project=entry.key,
            )
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="start_library_file_upload",
        title="Start Library File Upload",
        description=(
            "Start a chunked upload for a library file that needs to arrive in "
            "pieces (e.g. too large or too repetitive-looking to reliably embed "
            "in one content_base64 string). Declares filename, folder_path "
            "(under library/, like upload_library_file), total_size, "
            "total_chunks, and optionally mime_type/metadata/expected_sha256 "
            "(the original file's sha256, verified at finalize — catches "
            "corruption instead of silently landing it). Returns an upload_id: "
            "send the bytes via repeated append_upload_chunk calls "
            "(chunk_size_hint bytes decoded per chunk, currently 256 KB), then "
            "call finalize_library_file_upload. Multiple uploads may be open "
            "at once, each with its own upload_id — interleave freely. Pending "
            "for 24h; abandon with discard_upload."
        ),
        annotations=proposal_annotations(),
        structured_output=False,
    )
    def start_library_file_upload_tool(
        project: Annotated[str, _PROJECT_FIELD],
        filename: Annotated[
            str,
            Field(
                description="Real filename with extension, e.g. 'q3-report.pdf'",
                min_length=1,
                max_length=writes.MAX_UPLOAD_FILENAME_BYTES,
            ),
        ],
        total_size: Annotated[
            int,
            Field(
                description="Total decoded file size in bytes",
                ge=0,
                le=writes.MAX_UPLOAD_BYTES,
            ),
        ],
        total_chunks: Annotated[
            int,
            Field(
                description="How many append_upload_chunk calls",
                ge=1,
                le=writes.MAX_UPLOAD_CHUNKS,
            ),
        ],
        folder_path: Annotated[
            str,
            Field(description="Must be under library/, may nest, e.g. 'library/attachments'"),
        ] = "library",
        mime_type: Annotated[
            str | None,
            Field(
                description="MIME type of the file, if known", max_length=writes.MAX_MIME_TYPE_CHARS
            ),
        ] = None,
        metadata: Annotated[
            JsonObject | None,
            Field(description="Freeform metadata to store in the sidecar JSON; agent-authored"),
        ] = None,
        expected_sha256: Annotated[
            str | None,
            Field(
                description="sha256 of the original file; verified at finalize if given",
                pattern=r"^[0-9a-fA-F]{64}$",
            ),
        ] = None,
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.start_library_file_upload(
                    conn,
                    require_workspace(),
                    entry.key,
                    filename=filename,
                    total_size=total_size,
                    total_chunks=total_chunks,
                    folder_path=folder_path,
                    mime_type=mime_type,
                    metadata=metadata,
                    expected_sha256=expected_sha256,
                )
            finally:
                conn.close()
            return make_success(
                {
                    "upload_id": result.upload_id,
                    "expires_at": result.expires_at,
                    "chunk_size_hint": result.chunk_size_hint,
                    "next_required_tool": "append_upload_chunk",
                },
                project=entry.key,
            )
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="append_upload_chunk",
        title="Append Upload Chunk",
        description=(
            "Send one chunk of a pending upload_id from "
            "start_library_file_upload. chunk_index is 0-based; resending the "
            "same index overwrites it, so a retried chunk is safe. Each chunk "
            "is capped at 256 KB decoded. Returns progress (received_chunks of "
            "total_chunks); call finalize_library_file_upload once all are in."
        ),
        annotations=proposal_annotations(),
        structured_output=False,
    )
    def append_upload_chunk_tool(
        project: Annotated[str, _PROJECT_FIELD],
        upload_id: Annotated[str, Field(description="upload_id from start_library_file_upload")],
        chunk_index: Annotated[
            int,
            Field(description="0-based chunk index", ge=0, lt=writes.MAX_UPLOAD_CHUNKS),
        ],
        chunk_base64: Annotated[
            str,
            Field(
                description="This chunk's raw bytes, base64-encoded",
                max_length=_MAX_BASE64_CHUNK_CHARS,
            ),
        ],
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.append_upload_chunk(
                    conn,
                    require_workspace(),
                    entry.key,
                    upload_id=upload_id,
                    chunk_index=chunk_index,
                    chunk_base64=chunk_base64,
                )
            finally:
                conn.close()
            return make_success(
                {
                    "upload_id": result.upload_id,
                    "received_chunks": result.received_chunks,
                    "total_chunks": result.total_chunks,
                    "received_bytes": result.received_bytes,
                    "complete": result.received_chunks == result.total_chunks,
                },
                project=entry.key,
            )
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="finalize_library_file_upload",
        title="Finalize Library File Upload",
        description=(
            "Assemble a completed chunked upload and write it to library/ — "
            "same result shape as upload_library_file. Fails with "
            "UPLOAD_INCOMPLETE if any chunk is missing, VALIDATION_ERROR if "
            "the assembled size doesn't match what start_library_file_upload "
            "declared, and CONTENT_HASH_MISMATCH if expected_sha256 was given "
            "and doesn't match — nothing is written in any failure case."
            "Images (JPEG/PNG/WebP) are normalized for storage before they are "
            "written: downscaled to the workspace's configured longest edge and "
            "re-encoded, so the stored file is usually much smaller than what you "
            "sent. The full-resolution original is NOT retained. A lossless source "
            "(PNG, lossless WebP) stays lossless and pixel-identical, and nothing "
            "is ever made larger. Because of this the returned sha256 and "
            "size_bytes describe the STORED bytes, not the bytes you uploaded — "
            "do not treat a difference as a transfer error."
        ),
        annotations=write_annotations(),
        structured_output=False,
    )
    def finalize_library_file_upload_tool(
        project: Annotated[str, _PROJECT_FIELD],
        upload_id: Annotated[str, Field(description="upload_id from start_library_file_upload")],
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.finalize_library_file_upload(
                    conn,
                    require_workspace(),
                    entry.key,
                    upload_id=upload_id,
                    image_policy=require_config().image_policy,
                )
            finally:
                conn.close()
            return make_success(
                {
                    "operation_id": result.operation_id,
                    "snapshot_id": result.snapshot_id,
                    "path": result.path,
                    "metadata_path": result.metadata_path,
                    "folder": result.folder,
                    "sha256": result.sha256,
                    "size_bytes": result.size_bytes,
                    "mime_type": result.mime_type,
                    "document_mutated": True,
                },
                project=entry.key,
            )
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="discard_upload",
        title="Discard Upload",
        description=(
            "Abandon a pending chunked upload session and delete its staged "
            "chunks. Only affects .ferumind/ scratch storage; nothing was ever "
            "written to library/ by a discarded upload."
        ),
        annotations=proposal_annotations(),
        structured_output=False,
    )
    def discard_upload_tool(
        project: Annotated[str, _PROJECT_FIELD],
        upload_id: Annotated[str, Field(description="upload_id from start_library_file_upload")],
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.discard_upload(
                    conn, require_workspace(), entry.key, upload_id=upload_id
                )
            finally:
                conn.close()
            return make_success(
                {"upload_id": result.upload_id, "path": result.path, "state": result.state},
                project=entry.key,
            )
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="upload_library_files_from_chatgpt",
        title="Upload Library Files From ChatGPT",
        description=(
            "Upload one or more files into library/ that ChatGPT has already "
            "resolved from the conversation or the user's File Library — the "
            "server downloads each file's download_url directly, so the model "
            "never has to reproduce file bytes as text (unlike "
            "upload_library_file/append_upload_chunk). Not a local filesystem "
            "path and not a generic MCP attachment: download_url is a "
            "temporary, authorized URL, fetched once during this call and "
            "never stored. Every file gets its own result "
            "(ok/path/error_code/error_message) — one bad file does not lose "
            "the others; check succeeded/failed for partial-batch outcomes. "
            "Generic (non-ChatGPT) MCP clients should use upload_library_file "
            "or the chunked tools instead; this tool depends on ChatGPT's "
            "optional openai/fileParams host extension."
            "Images (JPEG/PNG/WebP) are normalized for storage before they are "
            "written: downscaled to the workspace's configured longest edge and "
            "re-encoded, so the stored file is usually much smaller than what you "
            "sent. The full-resolution original is NOT retained. A lossless source "
            "(PNG, lossless WebP) stays lossless and pixel-identical, and nothing "
            "is ever made larger. Because of this the returned sha256 and "
            "size_bytes describe the STORED bytes, not the bytes you uploaded — "
            "do not treat a difference as a transfer error."
        ),
        annotations=open_world_write_annotations(),
        structured_output=False,
        meta={"openai/fileParams": ["files"]},
    )
    async def upload_library_files_from_chatgpt_tool(
        project: Annotated[str, _PROJECT_FIELD],
        files: Annotated[
            list[writes.ChatGPTFileInput],
            Field(
                description="ChatGPT file references to download and store",
                min_length=1,
                max_length=writes.MAX_CHATGPT_FILES_PER_CALL,
            ),
        ],
        folder_path: Annotated[
            str,
            Field(description="Must be under library/, may nest, e.g. 'library/attachments'"),
        ] = "library",
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            workspace = require_workspace()
            # Bound here: the closure runs on a worker thread, where the
            # request-scoped tool context is not available.
            image_policy = require_config().image_policy

            def run_upload() -> writes.ChatGPTBatchUploadResult:
                conn = db.get_connection()
                try:
                    return writes.upload_library_files_from_chatgpt(
                        conn,
                        workspace,
                        entry.key,
                        files=files,
                        folder_path=folder_path,
                        image_policy=image_policy,
                    )
                finally:
                    conn.close()

            result = await run_sync(
                run_upload,
                limiter=remote_download_limiter,
            )
            return make_success(
                {
                    "results": [r.model_dump(mode="json") for r in result.results],
                    "succeeded": result.succeeded,
                    "failed": result.failed,
                },
                project=entry.key,
            )
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="upload_library_file_from_chatgpt",
        title="Upload Library File From ChatGPT",
        description=(
            "Upload ONE file that ChatGPT has already resolved from the "
            "conversation or the user's File Library into library/, stored "
            "under a filename you choose. Use this whenever the destination "
            "filename matters: one file per call is the only way to be sure a "
            "chosen name lands on the file you meant, because the file "
            "reference is filled in by ChatGPT after you write the call — you "
            "cannot name a specific file in another argument. To store several "
            "files under chosen names, make several calls (they are "
            "independent and may run in parallel). Use "
            "upload_library_files_from_chatgpt instead only when ChatGPT's own "
            "filenames are acceptable. The server downloads download_url "
            "directly, so the model never reproduces file bytes as text; the "
            "URL is temporary, authorized, fetched once, and never stored. "
            "Generic (non-ChatGPT) MCP clients should use upload_library_file "
            "or the chunked tools instead."
            "Images (JPEG/PNG/WebP) are normalized for storage before they are "
            "written: downscaled to the workspace's configured longest edge and "
            "re-encoded, so the stored file is usually much smaller than what you "
            "sent. The full-resolution original is NOT retained. A lossless source "
            "(PNG, lossless WebP) stays lossless and pixel-identical, and nothing "
            "is ever made larger. Because of this the returned sha256 and "
            "size_bytes describe the STORED bytes, not the bytes you uploaded — "
            "do not treat a difference as a transfer error."
        ),
        annotations=open_world_write_annotations(),
        structured_output=False,
        meta={"openai/fileParams": ["file"]},
    )
    async def upload_library_file_from_chatgpt_tool(
        project: Annotated[str, _PROJECT_FIELD],
        file: Annotated[
            writes.ChatGPTFileInput,
            Field(description="The single ChatGPT file reference to download and store"),
        ],
        filename: Annotated[
            str,
            Field(
                description=(
                    "Destination filename for this file — a bare name with an "
                    "extension, no path separators (e.g. 'flex-01-front.jpg')"
                ),
                min_length=1,
                max_length=writes.MAX_UPLOAD_FILENAME_BYTES,
            ),
        ],
        folder_path: Annotated[
            str,
            Field(description="Must be under library/, may nest, e.g. 'library/attachments'"),
        ] = "library",
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            workspace = require_workspace()
            # Bound here: the closure runs on a worker thread, where the
            # request-scoped tool context is not available.
            image_policy = require_config().image_policy

            def run_upload() -> writes.ChatGPTSingleUploadResult:
                conn = db.get_connection()
                try:
                    return writes.upload_library_file_from_chatgpt(
                        conn,
                        workspace,
                        entry.key,
                        file=file,
                        filename=filename,
                        folder_path=folder_path,
                        image_policy=image_policy,
                    )
                finally:
                    conn.close()

            result = await run_sync(
                run_upload,
                limiter=remote_download_limiter,
            )
            return make_success(result.model_dump(mode="json"), project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="capture_note",
        title="Capture Note",
        description="Capture a stray thought into the project's inbox/ for later triage.",
        annotations=write_annotations(),
        structured_output=False,
    )
    def capture_note_tool(
        project: Annotated[str, _PROJECT_FIELD],
        text: Annotated[str, Field(description="Note content", min_length=1)],
        title: Annotated[str | None, Field(description="Optional note title")] = None,
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.capture_note(
                    conn, require_workspace(), entry.key, text=text, title=title
                )
            finally:
                conn.close()
            data = _write_result_data(result)
            data["document_mutated"] = True
            return make_success(data, project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="archive_document",
        title="Archive Document",
        description=(
            "Archive a document: sets status: archived and moves it to "
            "archive/<original-path>. Snapshot-protected; refuses the spine. "
            "Archived documents vanish from get_context and default search."
        ),
        annotations=write_annotations(),
        structured_output=False,
    )
    def archive_document_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, Field(description="Project-relative Markdown path")],
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.archive_document(conn, require_workspace(), entry.key, path=path)
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
        structured_output=False,
    )
    def unarchive_document_tool(
        project: Annotated[str, _PROJECT_FIELD],
        archived_path: Annotated[
            str, Field(description="Path under archive/, e.g. archive/canvases/plan.md")
        ],
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.unarchive_document(
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
            "first, so the restore itself is reversible."
        ),
        annotations=write_annotations(),
        structured_output=False,
    )
    def restore_snapshot_tool(
        project: Annotated[str, _PROJECT_FIELD],
        snapshot_id: Annotated[str, Field(description="Snapshot id from list_snapshots")],
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.restore_snapshot(conn, require_workspace(), entry.key, snapshot_id)
            finally:
                conn.close()
            data = _write_result_data(result)
            data["restored_from_snapshot_id"] = result.restored_from_snapshot_id
            data["rollback_snapshot_id"] = result.rollback_snapshot_id
            data["document_mutated"] = True
            return make_success(data, project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="create_project",
        title="Create Project",
        description=(
            "Workspace-level: register a new project and seed its spine + folder "
            "skeleton from system/templates/. No project argument."
        ),
        annotations=write_annotations(),
        structured_output=False,
    )
    def create_project_tool(
        key: Annotated[
            str,
            Field(
                description="Project key: lowercase letters, digits, hyphens; starts with a letter"
            ),
        ],
        title: Annotated[str, Field(description="Project title", min_length=1)],
    ) -> FerumindToolResult:
        try:
            require_format_gate().check_write()
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.create_project(conn, require_workspace(), key=key, title=title)
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
            "Rebuild the derived search index from the Markdown on disk, for one "
            "project or all projects. Safe anytime: the index is derived state."
        ),
        annotations=write_annotations(),
        structured_output=False,
    )
    def rebuild_index_tool(
        project: Annotated[
            str | None, Field(description="Project key, or omit to rebuild every project")
        ] = None,
    ) -> FerumindToolResult:
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

    _pin_chatgpt_file_schema(mcp)
