"""Standard MCP resource handling for ``ferumind://file/...`` URIs (spec-mcp §5.4).

Tier 2 of the file surface: a client that wants the *original* file — not a
rendition — issues ``resources/read`` against the URI ``list_files`` and
``read_file`` hand out. This module resolves that URI back to exact bytes.

Two SDK notes, both deliberate:

* **The read handler is registered at the low level, not through
  ``@mcp.resource``.** FastMCP's ``ResourceTemplate`` carries a single fixed
  ``mime_type`` for every resource it creates, so a template could only ever
  label a JPEG and a PDF with the same type. Per-file MIME is a hard
  requirement here, so the low-level handler owns the read. Non-Ferumind URIs
  fall through to FastMCP so nothing else is broken by the override.
* **A template is still registered** so ``resources/templates/list``
  advertises the URI shape to clients that browse it. Concrete resources are
  deliberately *not* enumerated: a project can hold thousands of files, and
  ``resources/list`` is not a paginated discovery channel. ``list_files`` is.
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import cast

from mcp import types
from mcp.server.fastmcp.server import FastMCP
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from ferumind.core.errors import FerumindError
from ferumind.core.file_reads import FileResourceContent, read_file_resource
from ferumind.core.file_uri import FILE_URI_PREFIX, parse_file_uri
from ferumind.core.locks import acquire_project_lock
from ferumind.core.observations import new_correlation_id, record_mcp_call_observation
from ferumind.core.paths import PathSafetyError, contained_project_root
from ferumind.core.types import JsonObject
from ferumind.mcp.tool_context import (
    current_transport,
    require_config,
    require_database,
    require_format_gate,
    require_workspace,
    scoped_project,
)

logger = logging.getLogger(__name__)

RESOURCE_TEMPLATE_URI = f"{FILE_URI_PREFIX}{{project}}/{{encoded_path}}"

_RESOURCE_TOOL_NAME = "resources/read"

_RESOURCE_TEMPLATE_DESCRIPTION = (
    "The untouched original of a project file. Build the URI from the "
    "resource_uri field returned by list_files or read_file rather than "
    "assembling it by hand; the encoded path segment has one canonical form "
    "and non-canonical spellings are rejected. Originals are returned whole "
    "and are never truncated, so a large file can exceed what the connection "
    "will carry: such a read fails with FILE_TOO_LARGE rather than returning "
    "a partial result. When it does, call read_file on the same path for a "
    "bounded rendition or text slice instead of retrying this URI."
)


def _record_resource_observation(
    *,
    project_key: str | None,
    ok: bool,
    error_code: str | None,
    duration_ms: float,
    result_bytes: int | None,
    metrics: JsonObject | None,
) -> None:
    """Record a resource read at the same metadata level as a tool call.

    Metadata only — MIME type and byte counts, never the blob itself.
    Telemetry failures are swallowed so they can never break a read.
    """
    try:
        db = require_database()
        conn = db.get_connection()
        try:
            record_mcp_call_observation(
                conn,
                tool_name=_RESOURCE_TOOL_NAME,
                correlation_id=new_correlation_id(),
                project_key=project_key,
                ok=ok,
                error_code=error_code,
                transport=current_transport(),
                argument_keys=["uri"],
                context_metrics=metrics,
                duration_ms=duration_ms,
                result_bytes=result_bytes,
            )
        finally:
            conn.close()
    except Exception as exc:  # observation must never break a resource read
        logger.error("Failed to record resource observation (type=%s)", type(exc).__name__)


def _resource_error(code: str, message: str, details: JsonObject | None = None) -> McpError:
    """Build a JSON-RPC error carrying the Ferumind error code in ``data``.

    ``resources/read`` has no Ferumind envelope, so the machine-readable code
    rides in the error's structured ``data`` instead.
    """
    data: JsonObject = {"error_code": code}
    if details:
        data.update(details)
    return McpError(types.ErrorData(code=types.INVALID_PARAMS, message=message, data=data))


def _contents_for(content: FileResourceContent) -> list[ReadResourceContents]:
    """Map core content onto the SDK's read-resource carrier.

    ``str`` becomes ``TextResourceContents`` and ``bytes`` becomes
    ``BlobResourceContents`` (base64-encoded by the SDK) in the low-level
    handler, so the type of ``content`` decides the wire shape.
    """
    if content.text is not None:
        return [ReadResourceContents(content=content.text, mime_type=content.mime_type)]
    return [ReadResourceContents(content=content.blob or b"", mime_type=content.mime_type)]


def read_ferumind_file_resource(uri_text: str) -> tuple[list[ReadResourceContents], JsonObject]:
    """Resolve one ``ferumind://file/...`` URI to the original file's contents.

    Every read revalidates from scratch — the server is stateless per call,
    so a URI minted an hour ago is re-checked against the registry and the
    path helpers exactly as a fresh one would be.
    """
    require_format_gate().check_read()
    parsed = parse_file_uri(uri_text)
    entry = scoped_project(parsed.project_key)
    workspace = require_workspace()
    project_root = contained_project_root(workspace, entry.key)
    with acquire_project_lock(project_root, entry.key):
        content = read_file_resource(
            workspace,
            entry.key,
            parsed.path,
            max_response_bytes=require_config().max_resource_response_bytes,
        )
    metrics: JsonObject = {
        "mime_type": content.mime_type,
        "size_bytes": content.size_bytes,
        "kind": "text" if content.text is not None else "blob",
    }
    return _contents_for(content), metrics


def register_file_resources(mcp: FastMCP) -> None:
    """Register the file resource template and the low-level read handler."""

    @mcp.resource(
        RESOURCE_TEMPLATE_URI,
        name="ferumind_file",
        title="Ferumind Project File",
        description=_RESOURCE_TEMPLATE_DESCRIPTION,
    )
    def ferumind_file_resource(project: str, encoded_path: str) -> str | bytes:
        """Serve a project file's original bytes.

        Registered so the URI shape is advertised in
        ``resources/templates/list``. Reads are served by the low-level
        handler below, which can set the per-file MIME type a FastMCP
        template cannot; this body is the correct fallback if that
        override is ever absent.
        """
        contents, _metrics = read_ferumind_file_resource(
            f"{FILE_URI_PREFIX}{project}/{encoded_path}"
        )
        first = contents[0]
        return first.content

    # FastMCP installed its own ReadResourceRequest handler during
    # construction; registering here replaces it. Private access is required
    # because FastMCP exposes no public hook for the low-level server.
    lowlevel = mcp._mcp_server  # pyright: ignore[reportPrivateUsage]

    @lowlevel.read_resource()
    async def handle_read_resource(uri: AnyUrl) -> Iterable[ReadResourceContents]:
        uri_text = str(uri)
        if not uri_text.startswith(FILE_URI_PREFIX):
            # Not ours: preserve whatever FastMCP would have served.
            return await mcp.read_resource(uri)

        started = time.perf_counter()
        project_key: str | None = None
        try:
            project_key = parse_file_uri(uri_text).project_key
        except FerumindError:
            project_key = None

        try:
            contents, metrics = read_ferumind_file_resource(uri_text)
        except FerumindError as exc:
            _record_resource_observation(
                project_key=project_key,
                ok=False,
                error_code=exc.code,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                result_bytes=None,
                metrics=None,
            )
            raise _resource_error(exc.code, str(exc), exc.details) from exc
        except PathSafetyError as exc:
            _record_resource_observation(
                project_key=project_key,
                ok=False,
                error_code="WORKSPACE_MISMATCH",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                result_bytes=None,
                metrics=None,
            )
            # Never echo the resolved absolute path back to the client.
            raise _resource_error(
                "WORKSPACE_MISMATCH",
                "Resource path is outside the configured workspace boundary",
            ) from exc

        size = cast("int", metrics.get("size_bytes") or 0)
        _record_resource_observation(
            project_key=project_key,
            ok=True,
            error_code=None,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            result_bytes=size,
            metrics=metrics,
        )
        return contents
