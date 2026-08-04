"""MCP generic file tools: ``list_files`` and ``read_file`` (spec-mcp §5.4).

These are the non-Markdown half of the read surface. ``list_files``
discovers arbitrary files anywhere inside a project; ``read_file`` places a
supported representation of one file into model context and always hands
back a ``ResourceLink`` for the untouched original.

Both are read-only: they never mutate user content, and ``read_file``
re-encodes into a fresh buffer rather than touching the source file.
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import base64
from typing import Annotated

from mcp.types import CallToolResult, ContentBlock, ImageContent, ResourceLink, TextContent
from pydantic import AnyUrl, Field

from lattice.core.errors import LatticeError
from lattice.core.file_reads import (
    DEFAULT_MAX_TEXT_CHARS,
    MAX_TEXT_CHARS_LIMIT,
    MIN_TEXT_CHARS,
    FileContextResult,
    read_file_for_context,
)
from lattice.core.files import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    MIN_LIST_LIMIT,
    list_project_files,
)
from lattice.core.locks import acquire_project_lock
from lattice.core.paths import PathSafetyError, contained_project_root
from lattice.core.renditions import (
    DEFAULT_IMAGE_EDGE,
    DEFAULT_IMAGE_QUALITY,
    MAX_IMAGE_EDGE,
    MAX_IMAGE_QUALITY,
    MAX_IMAGE_RENDITION_BYTES,
    MIN_IMAGE_EDGE,
    MIN_IMAGE_QUALITY,
)
from lattice.core.types import JsonObject, JsonValue
from lattice.mcp.models import make_rich_success, make_success, read_only_annotations
from lattice.mcp.protocols import ToolRegistrar
from lattice.mcp.tool_context import (
    error_result,
    require_format_gate,
    require_workspace,
    scoped_project,
)

type LatticeToolResult = CallToolResult

_PROJECT_FIELD = Field(description="Project key; validated against the registry, never an override")

_LIST_FILES_DESCRIPTION = (
    "Discover non-Markdown files anywhere inside a project — photographs, "
    "PDFs, spreadsheets, exports — by walking the project on demand. "
    "Workflow: read the project's rules, spine, and documents first, since "
    "they carry the workspace's own conventions and often reference files "
    "by path; call list_files when you do not already know the exact path; "
    "then pass the path you chose to read_file. There is no prescribed "
    "folder for files — a file may live in any valid nested location, and "
    "library/ is only where uploads happen to land. Filenames, folders, and "
    "extensions describe transport, never meaning: do not infer what a file "
    "depicts from where it sits. query is a literal case-insensitive "
    "substring match over path, filename, MIME type, extension, and any "
    "scalar values in a Lattice-generated upload sidecar — binary content "
    "itself is NOT indexed or searched. Markdown is excluded by default "
    "(use search_project/list_tree/read_document for managed documents), as "
    "are Lattice upload sidecars and internal .lattice/ paths. Every result "
    "carries a project-relative path and a lattice:// resource_uri usable "
    "with resources/read."
)

_READ_FILE_DESCRIPTION = (
    "Read one project file into model context by its project-relative path, "
    "and return a resource link to the untouched original. For JPEG, PNG, "
    "and WebP the result carries a real image block holding a bounded "
    "rendition the server generated (EXIF-oriented, aspect preserved, never "
    "upscaled, metadata stripped). The edge and quality parameters are upper "
    "bounds; every image still obeys a hard encoded-byte ceiling so retrying "
    "with larger values cannot exceed a web host's tool-result limit. The "
    "original bytes never travel inline. For UTF-8 "
    "text the result carries a bounded slice with text_offset/max_text_chars "
    "paging. Everything else — PDF, Office documents, archives, video, GIF, "
    "SVG — is resource_only: you get metadata and the resource link, and you "
    "have NOT seen the contents. Lattice does not extract PDF text, render "
    "PDF pages, parse Office documents, or run OCR. The returned "
    "resource_uri always represents the exact original; whether a linked "
    "resource is attached to the conversation is the client's decision, not "
    "the server's. Managed Markdown is served as plain text here, but "
    "read_document is the right tool for it — it returns frontmatter and the "
    "document hash that hash-guarded edits need. "
    "This tool is also the fallback when resources/read on the same path "
    "fails with FILE_TOO_LARGE: originals are served whole and a large one "
    "can exceed what the connection will carry, whereas the rendition and "
    "text slice returned here are bounded by construction and always fit."
)


def _resource_link(result: FileContextResult) -> ResourceLink:
    """Build the standard MCP link to the untouched original."""
    return ResourceLink(
        type="resource_link",
        uri=AnyUrl(result.file.resource_uri),
        name=result.file.path.rsplit("/", 1)[-1],
        title=result.file.path,
        description=f"Original {result.file.mime_type} file at {result.file.path}",
        mimeType=result.file.mime_type,
        size=result.file.size_bytes,
    )


def _base_metadata(result: FileContextResult) -> JsonObject:
    """Envelope metadata shared by every representation.

    Deliberately excludes any encoded payload: the rendition's bytes exist
    only inside the ``ImageContent`` block.
    """
    original: JsonObject = {
        "mime_type": result.file.mime_type,
        "size_bytes": result.file.size_bytes,
        "sha256": result.sha256,
    }
    if result.rendition is not None:
        original["width"] = result.rendition.original_width
        original["height"] = result.rendition.original_height
    data: JsonObject = {
        "path": result.file.path,
        "resource_uri": result.file.resource_uri,
        "representation": result.representation,
        "context_support": result.file.context_support,
        "is_markdown": result.file.is_markdown,
        "original": original,
    }
    if result.file.is_markdown:
        data["recommended_tool"] = "read_document"
    if result.reason is not None:
        data["reason"] = result.reason
    if result.sidecar is not None:
        data["sidecar"] = {
            "path": result.sidecar.path,
            "metadata": result.sidecar.metadata,
        }
    return data


def _image_blocks(result: FileContextResult) -> tuple[JsonObject, list[ContentBlock]]:
    rendition = result.rendition
    if rendition is None:  # pragma: no cover - guarded by representation
        raise LatticeError("Image representation is missing its rendition")
    data = _base_metadata(result)
    rendition_data: JsonObject = {
        "mime_type": rendition.mime_type,
        "size_bytes": rendition.size_bytes,
        "width": rendition.width,
        "height": rendition.height,
        "resized": rendition.resized,
        "size_limited": rendition.size_limited,
        "size_limit_bytes": MAX_IMAGE_RENDITION_BYTES,
    }
    data["rendition"] = rendition_data
    if rendition.encode_quality is not None:
        rendition_data["encode_quality"] = rendition.encode_quality
    limited_note = (
        " The encoded-byte limit reduced the requested quality or geometry."
        if rendition.size_limited
        else ""
    )
    summary = (
        f"{result.file.path} — {result.file.mime_type}, "
        f"{rendition.original_width}x{rendition.original_height}, "
        f"{result.file.size_bytes} bytes. Showing a {rendition.width}x{rendition.height} "
        f"{rendition.mime_type} rendition ({rendition.size_bytes} bytes, capped at "
        f"{MAX_IMAGE_RENDITION_BYTES} bytes).{limited_note} The original is at "
        f"{result.file.resource_uri}."
    )
    blocks: list[ContentBlock] = [
        TextContent(type="text", text=summary),
        ImageContent(
            type="image",
            data=base64.b64encode(rendition.data).decode("ascii"),
            mimeType=rendition.mime_type,
        ),
        _resource_link(result),
    ]
    return data, blocks


def _text_blocks(result: FileContextResult) -> tuple[JsonObject, list[ContentBlock]]:
    text_slice = result.text
    if text_slice is None:  # pragma: no cover - guarded by representation
        raise LatticeError("Text representation is missing its slice")
    data = _base_metadata(result)
    data["text"] = {
        "offset": text_slice.offset,
        "returned_chars": text_slice.returned_chars,
        "total_chars": text_slice.total_chars,
        "truncated": text_slice.truncated,
        "next_offset": text_slice.next_offset,
    }
    span_end = text_slice.offset + text_slice.returned_chars
    span = f"characters {text_slice.offset}-{span_end}"
    tail = (
        f" Truncated; call again with text_offset={text_slice.next_offset} for more."
        if text_slice.truncated
        else ""
    )
    summary = (
        f"{result.file.path} — {result.file.mime_type}, {span} of "
        f"{text_slice.total_chars}.{tail} Original at {result.file.resource_uri}."
    )
    blocks: list[ContentBlock] = [
        TextContent(type="text", text=summary),
        TextContent(type="text", text=text_slice.text),
        _resource_link(result),
    ]
    return data, blocks


def _resource_only_blocks(result: FileContextResult) -> tuple[JsonObject, list[ContentBlock]]:
    data = _base_metadata(result)
    summary = (
        f"{result.file.path} — {result.file.mime_type}, {result.file.size_bytes} bytes. "
        "Lattice has no model-context rendition for this type, so its contents have "
        f"not been read. The original is at {result.file.resource_uri}."
    )
    blocks: list[ContentBlock] = [
        TextContent(type="text", text=summary),
        _resource_link(result),
    ]
    return data, blocks


def register_file_tools(mcp: ToolRegistrar) -> None:
    """Register the generic file discovery and retrieval tools."""

    @mcp.tool(
        name="list_files",
        title="List Files",
        description=_LIST_FILES_DESCRIPTION,
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def list_files_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path_prefix: Annotated[
            str | None,
            Field(description="Restrict to a project-relative directory prefix"),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description=(
                    "Case-insensitive substring over path, filename, MIME type, "
                    "extension, and upload-sidecar scalars"
                ),
                max_length=512,
            ),
        ] = None,
        mime_type: Annotated[
            str | None,
            Field(description="Exact MIME type filter, e.g. 'image/jpeg'", max_length=255),
        ] = None,
        extension: Annotated[
            str | None,
            Field(description="Extension filter with or without the dot, e.g. '.jpg'"),
        ] = None,
        include_markdown: Annotated[
            bool,
            Field(
                description="Include managed Markdown documents (normally read via read_document)"
            ),
        ] = False,
        include_sidecars: Annotated[
            bool,
            Field(description="Include Lattice-generated upload metadata sidecars"),
        ] = False,
        limit: Annotated[
            int,
            Field(description="Maximum results", ge=MIN_LIST_LIMIT, le=MAX_LIST_LIMIT),
        ] = DEFAULT_LIST_LIMIT,
        cursor: Annotated[
            str | None,
            Field(description="next_cursor from a previous call"),
        ] = None,
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_read()
            entry = scoped_project(project)
            project_root = contained_project_root(require_workspace(), entry.key)
            with acquire_project_lock(project_root, entry.key):
                listing = list_project_files(
                    project_root,
                    entry.key,
                    path_prefix=path_prefix,
                    query=query,
                    mime_type=mime_type,
                    extension=extension,
                    include_markdown=include_markdown,
                    include_sidecars=include_sidecars,
                    limit=limit,
                    cursor=cursor,
                )
            files: list[JsonValue] = [
                item.model_dump(mode="json", exclude_none=True) for item in listing.files
            ]
            return make_success(
                {
                    "files": files,
                    "count": listing.count,
                    "has_more": listing.has_more,
                    "next_cursor": listing.next_cursor,
                    "scanned_count": listing.scanned_count,
                },
                project=entry.key,
            )
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="read_file",
        title="Read File",
        description=_READ_FILE_DESCRIPTION,
        annotations=read_only_annotations(),
        structured_output=False,
    )
    def read_file_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, Field(description="Project-relative file path from list_files")],
        max_image_edge: Annotated[
            int,
            Field(
                description="Longest edge of the image rendition in pixels",
                ge=MIN_IMAGE_EDGE,
                le=MAX_IMAGE_EDGE,
            ),
        ] = DEFAULT_IMAGE_EDGE,
        image_quality: Annotated[
            int,
            Field(
                description=(
                    "Preferred lossy encode quality; the hard rendition byte cap takes precedence"
                ),
                ge=MIN_IMAGE_QUALITY,
                le=MAX_IMAGE_QUALITY,
            ),
        ] = DEFAULT_IMAGE_QUALITY,
        text_offset: Annotated[
            int,
            Field(description="First character to return for text files", ge=0),
        ] = 0,
        max_text_chars: Annotated[
            int,
            Field(
                description="Maximum characters to return for text files",
                ge=MIN_TEXT_CHARS,
                le=MAX_TEXT_CHARS_LIMIT,
            ),
        ] = DEFAULT_MAX_TEXT_CHARS,
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_read()
            entry = scoped_project(project)
            workspace = require_workspace()
            project_root = contained_project_root(workspace, entry.key)
            with acquire_project_lock(project_root, entry.key):
                result = read_file_for_context(
                    workspace,
                    entry.key,
                    path,
                    max_image_edge=max_image_edge,
                    image_quality=image_quality,
                    text_offset=text_offset,
                    max_text_chars=max_text_chars,
                )
            if result.representation == "image":
                data, blocks = _image_blocks(result)
            elif result.representation == "text":
                data, blocks = _text_blocks(result)
            else:
                data, blocks = _resource_only_blocks(result)
            return make_rich_success(data, blocks, project=entry.key)
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)
