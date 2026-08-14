"""MCP upload tools: every way non-Markdown bytes reach a project's ``library/``.

Base64 in one call (``upload_library_file``), a chunked session for anything
larger (``start_library_file_upload`` / ``append_upload_chunk`` /
``finalize_library_file_upload`` / ``discard_upload``), and ChatGPT-resolved
file references the server fetches itself
(``upload_library_file(s)_from_chatgpt``). They share one core module,
:mod:`ferumind.core.upload_writes`, and therefore one set of guarantees:
extension denylist, fail-closed collision, metadata sidecar, snapshot,
operation log.

The chunked start/append/discard tools are annotated as proposal-shaped, not
content-mutating: nothing reaches ``library/`` until finalize. Every write here
is refused with ``FORMAT_UNSUPPORTED`` on a mismatched workspace format.
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

from typing import Annotated, Any, Final

import anyio
from anyio.to_thread import run_sync
from mcp.types import CallToolResult
from pydantic import Field

from ferumind.core import upload_writes
from ferumind.core.errors import FerumindError
from ferumind.core.paths import PathSafetyError
from ferumind.core.types import JsonObject
from ferumind.core.write_limits import (
    MAX_CHATGPT_FILES_PER_CALL,
    MAX_CHUNK_BYTES,
    MAX_MIME_TYPE_CHARS,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_CHUNKS,
    MAX_UPLOAD_FILENAME_BYTES,
)
from ferumind.mcp.models import (
    FerumindResult,
    make_success,
    open_world_write_annotations,
    proposal_annotations,
    write_annotations,
)
from ferumind.mcp.protocols import ToolRegistrar
from ferumind.mcp.result_models import (
    ChunkAppendData,
    DiscardUploadData,
    UploadData,
    UploadSessionData,
)
from ferumind.mcp.sdk_internals import registered_tools
from ferumind.mcp.tool_context import (
    error_result,
    require_config,
    require_database,
    require_format_gate,
    require_workspace,
    scoped_project,
)

_PROJECT_FIELD = Field(description="Project key; validated against the registry, never an override")
_MAX_BASE64_CHUNK_CHARS = ((MAX_CHUNK_BYTES + 2) // 3) * 4

#: Appended to every tool that writes an image to storage. Defined once: it was
#: previously duplicated verbatim across four descriptions, which is ~1.4 KB of
#: model context repeated on every tools/list. Leading space is required — these
#: are concatenated onto a preceding sentence.
_IMAGE_NORMALIZATION_NOTE = (
    " Stored images (JPEG/PNG/WebP) are downscaled to the workspace's longest-edge "
    "limit and re-encoded; the original resolution is not kept, and lossless sources "
    "stay lossless. Returned sha256/size_bytes therefore describe the stored bytes, "
    "not what you sent."
)

#: Shared tail for tools returning the standard write result. Defined once for
#: the same reason as _IMAGE_NORMALIZATION_NOTE. Leading space required.
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

    The SDK derives the ``upload_*_from_chatgpt`` input schemas from
    :class:`upload_writes.ChatGPTFileInput` automatically, which represents the
    optional ``mime_type``/``file_name`` fields as ``anyOf: [string, null]``
    (idiomatic pydantic/JSON-Schema) rather than the plain
    ``{"type": "string"}`` ChatGPT's own file-reference schema uses for
    every property. Runtime argument validation still goes through the
    pydantic model — a strict superset, since it also accepts an explicit
    ``null`` — so only the *advertised* ``tools/list`` schema needs
    normalizing here. Editing a registered tool's schema has no public API;
    reaching the registered ``Tool`` objects does.

    The batch tool declares an array of file references; the single-file
    tool declares one file reference directly (the same object schema, not
    wrapped in an array) — both must be top level, since the extension does
    not resolve nested file params.
    """
    tools = {tool.name: tool for tool in registered_tools(mcp)}

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
            # Replace in place: the description the SDK attached to the
            # parameter is dropped along with the $ref, matching the exact
            # item schema ChatGPT looks for.
            properties["file"] = dict(_CHATGPT_FILE_ITEM_SCHEMA)
        single.parameters.get("$defs", {}).pop("ChatGPTFileInput", None)


def _register_direct_upload_tool(mcp: ToolRegistrar) -> None:
    """The one-shot base64 upload: everything in a single tool call."""

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
            "path if you need to reference the file again." + _IMAGE_NORMALIZATION_NOTE
        ),
        annotations=write_annotations(),
    )
    def upload_library_file_tool(
        project: Annotated[str, _PROJECT_FIELD],
        filename: Annotated[
            str,
            Field(
                description="Real filename with extension, e.g. 'q3-report.pdf'",
                min_length=1,
                max_length=MAX_UPLOAD_FILENAME_BYTES,
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
            Field(description="MIME type of the file, if known", max_length=MAX_MIME_TYPE_CHARS),
        ] = None,
        metadata: Annotated[
            JsonObject | None,
            Field(description="Freeform metadata to store in the sidecar JSON; agent-authored"),
        ] = None,
    ) -> Annotated[CallToolResult, FerumindResult[UploadData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = upload_writes.upload_library_file(
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


def _register_chunked_upload_tools(mcp: ToolRegistrar) -> None:
    """The session tools, for a file too large to arrive in one call.

    Only ``finalize_library_file_upload`` publishes bytes; start, append and
    discard stage a pending session and are annotated accordingly.
    """

    @mcp.tool(
        name="start_library_file_upload",
        title="Start Library File Upload",
        description=(
            "Start a chunked upload for a library file too large for upload_library_file's "
            "one-shot 256 KB limit. Send bytes with repeated append_upload_chunk calls, "
            "then finalize_library_file_upload. expected_sha256 is the original file's "
            "hash, verified at finalize so corruption fails instead of landing silently. "
            "Uploads may be open concurrently and interleaved; each is pending 24 h and "
            "abandoned with discard_upload."
        ),
        annotations=proposal_annotations(),
    )
    def start_library_file_upload_tool(
        project: Annotated[str, _PROJECT_FIELD],
        filename: Annotated[
            str,
            Field(
                description="Real filename with extension, e.g. 'q3-report.pdf'",
                min_length=1,
                max_length=MAX_UPLOAD_FILENAME_BYTES,
            ),
        ],
        total_size: Annotated[
            int,
            Field(
                description="Total decoded file size in bytes",
                ge=0,
                le=MAX_UPLOAD_BYTES,
            ),
        ],
        total_chunks: Annotated[
            int,
            Field(
                description="How many append_upload_chunk calls",
                ge=1,
                le=MAX_UPLOAD_CHUNKS,
            ),
        ],
        folder_path: Annotated[
            str,
            Field(description="Must be under library/, may nest, e.g. 'library/attachments'"),
        ] = "library",
        mime_type: Annotated[
            str | None,
            Field(description="MIME type of the file, if known", max_length=MAX_MIME_TYPE_CHARS),
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
    ) -> Annotated[CallToolResult, FerumindResult[UploadSessionData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = upload_writes.start_library_file_upload(
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
            "Send one chunk of a pending upload_id from start_library_file_upload. "
            "chunk_index is 0-based; resending the same index overwrites it, so a retried "
            "chunk is safe. Each chunk is capped at 256 KB decoded. Call "
            "finalize_library_file_upload once all chunks are in."
        ),
        annotations=proposal_annotations(),
    )
    def append_upload_chunk_tool(
        project: Annotated[str, _PROJECT_FIELD],
        upload_id: Annotated[str, Field(description="upload_id from start_library_file_upload")],
        chunk_index: Annotated[
            int,
            Field(description="0-based chunk index", ge=0, lt=MAX_UPLOAD_CHUNKS),
        ],
        chunk_base64: Annotated[
            str,
            Field(
                description="This chunk's raw bytes, base64-encoded",
                max_length=_MAX_BASE64_CHUNK_CHARS,
            ),
        ],
    ) -> Annotated[CallToolResult, FerumindResult[ChunkAppendData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = upload_writes.append_upload_chunk(
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
            + _IMAGE_NORMALIZATION_NOTE
        ),
        annotations=write_annotations(),
    )
    def finalize_library_file_upload_tool(
        project: Annotated[str, _PROJECT_FIELD],
        upload_id: Annotated[str, Field(description="upload_id from start_library_file_upload")],
    ) -> Annotated[CallToolResult, FerumindResult[UploadData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = upload_writes.finalize_library_file_upload(
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
            "Abandon a pending chunked upload session and delete its staged chunks. Only "
            "affects .ferumind/ scratch storage; nothing was ever written to library/ by a "
            "discarded upload."
        ),
        annotations=proposal_annotations(),
    )
    def discard_upload_tool(
        project: Annotated[str, _PROJECT_FIELD],
        upload_id: Annotated[str, Field(description="upload_id from start_library_file_upload")],
    ) -> Annotated[CallToolResult, FerumindResult[DiscardUploadData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = upload_writes.discard_upload(
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


def _register_chatgpt_upload_tools(mcp: ToolRegistrar) -> None:
    """The two tools whose bytes the *server* fetches, never the model.

    Both offload the download to a worker thread behind a shared capacity
    limiter, so a slow remote file cannot monopolize the server.
    """

    remote_download_limiter = anyio.CapacityLimiter(2)

    @mcp.tool(
        name="upload_library_files_from_chatgpt",
        title="Upload Library Files From ChatGPT",
        description=(
            "Upload one or more ChatGPT-resolved files into library/ under ChatGPT's own "
            "filenames; use upload_library_file_from_chatgpt when the destination name "
            "matters. The server fetches each download_url itself — a temporary "
            "authorized URL, read once, never stored — so the model never reproduces "
            "file bytes as text. Not a local path and not a generic MCP attachment. "
            "Per-file failures are reported individually; one bad file "
            "does not lose the others, so check succeeded/failed for partial batches. "
            "Requires ChatGPT's openai/fileParams extension; other MCP clients use "
            "upload_library_file or the chunked tools." + _IMAGE_NORMALIZATION_NOTE
        ),
        annotations=open_world_write_annotations(),
        meta={"openai/fileParams": ["files"]},
    )
    async def upload_library_files_from_chatgpt_tool(
        project: Annotated[str, _PROJECT_FIELD],
        files: Annotated[
            list[upload_writes.ChatGPTFileInput],
            Field(
                description="ChatGPT file references to download and store",
                min_length=1,
                max_length=MAX_CHATGPT_FILES_PER_CALL,
            ),
        ],
        folder_path: Annotated[
            str,
            Field(description="Must be under library/, may nest, e.g. 'library/attachments'"),
        ] = "library",
    ) -> Annotated[CallToolResult, FerumindResult[upload_writes.ChatGPTBatchUploadResult]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            workspace = require_workspace()
            # Bound here: the closure runs on a worker thread, where the
            # request-scoped tool context is not available.
            image_policy = require_config().image_policy

            def run_upload() -> upload_writes.ChatGPTBatchUploadResult:
                conn = db.get_connection()
                try:
                    return upload_writes.upload_library_files_from_chatgpt(
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
            "Upload ONE ChatGPT-resolved file into library/ under a filename you choose. "
            "Use this whenever the destination name matters: ChatGPT fills the file "
            "reference in after you write the call, so one file per call is the only way "
            "to be sure a chosen name lands on the file you meant. For several named "
            "files, make several calls (independent, may run in parallel); use "
            "upload_library_files_from_chatgpt only when ChatGPT's own filenames are "
            "acceptable. The server fetches download_url itself — a temporary authorized "
            "URL, read once, never stored — so the model never reproduces file bytes as "
            "text. Requires ChatGPT's openai/fileParams extension; other MCP clients use "
            "upload_library_file or the chunked tools. Returns the same shape as "
            "upload_library_file." + _IMAGE_NORMALIZATION_NOTE
        ),
        annotations=open_world_write_annotations(),
        meta={"openai/fileParams": ["file"]},
    )
    async def upload_library_file_from_chatgpt_tool(
        project: Annotated[str, _PROJECT_FIELD],
        file: Annotated[
            upload_writes.ChatGPTFileInput,
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
                max_length=MAX_UPLOAD_FILENAME_BYTES,
            ),
        ],
        folder_path: Annotated[
            str,
            Field(description="Must be under library/, may nest, e.g. 'library/attachments'"),
        ] = "library",
    ) -> Annotated[CallToolResult, FerumindResult[upload_writes.ChatGPTSingleUploadResult]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            workspace = require_workspace()
            # Bound here: the closure runs on a worker thread, where the
            # request-scoped tool context is not available.
            image_policy = require_config().image_policy

            def run_upload() -> upload_writes.ChatGPTSingleUploadResult:
                conn = db.get_connection()
                try:
                    return upload_writes.upload_library_file_from_chatgpt(
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


def register_upload_tools(mcp: ToolRegistrar) -> None:
    """Register the upload tool family: direct, chunked, and ChatGPT-fetched."""
    _register_direct_upload_tool(mcp)
    _register_chunked_upload_tools(mcp)
    _register_chatgpt_upload_tools(mcp)
    _pin_chatgpt_file_schema(mcp)
