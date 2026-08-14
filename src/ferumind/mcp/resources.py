"""Standard MCP resource handling for ``ferumind://file/...`` URIs (spec-mcp §5.4).

Tier 2 of the file surface: a client that wants the *original* file — not a
rendition — issues ``resources/read`` against the URI ``list_files`` and
``read_file`` hand out. This module resolves that URI back to exact bytes.

Two SDK notes, both deliberate:

* **The read handler is registered on the low-level server, not through
  ``@mcp.resource``.** MCPServer's ``ResourceTemplate`` carries a single fixed
  ``mime_type`` for every resource it creates, so a template could only ever
  label a JPEG and a PDF with the same type. Per-file MIME is a hard
  requirement here, so this handler owns the read. Non-Ferumind URIs fall
  through to MCPServer so nothing else is broken by the override.
* **A template is still registered** so ``resources/templates/list``
  advertises the URI shape to clients that browse it. Concrete resources are
  deliberately *not* enumerated: a project can hold thousands of files, and
  ``resources/list`` is not a paginated discovery channel. ``list_files`` is.
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import base64
import logging
from collections.abc import Iterable

from mcp import types
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from ferumind.core.errors import FerumindError
from ferumind.core.file_reads import FileResourceContent, read_file_resource
from ferumind.core.file_uri import FILE_URI_PREFIX, parse_file_uri
from ferumind.core.locks import acquire_project_lock
from ferumind.core.paths import PathSafetyError, contained_project_root
from ferumind.core.types import JsonObject
from ferumind.mcp.sdk_internals import lowlevel_server
from ferumind.mcp.tool_context import (
    require_config,
    require_format_gate,
    require_workspace,
    scoped_project,
)

logger = logging.getLogger(__name__)

RESOURCE_TEMPLATE_URI = f"{FILE_URI_PREFIX}{{project}}/{{encoded_path}}"

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


def _resource_error(code: str, message: str, details: JsonObject | None = None) -> MCPError:
    """Build a JSON-RPC error carrying the Ferumind error code in ``data``.

    ``resources/read`` has no Ferumind envelope, so the machine-readable code
    rides in the error's structured ``data`` instead. mcp 2.x takes the fields
    directly rather than a wrapped ``ErrorData``.
    """
    data: JsonObject = {"error_code": code}
    if details:
        data.update(details)
    return MCPError(code=types.INVALID_PARAMS, message=message, data=data)


def _as_result(uri: str, contents: Iterable[ReadResourceContents]) -> types.ReadResourceResult:
    """Wrap read-resource carriers in the protocol result mcp 2.x expects.

    The SDK used to do this conversion behind the removed decorator. ``str``
    content becomes ``TextResourceContents`` and ``bytes`` becomes
    base64-encoded ``BlobResourceContents``, so the type of the content still
    decides the wire shape.
    """
    blocks: list[types.TextResourceContents | types.BlobResourceContents] = []
    for item in contents:
        if isinstance(item.content, bytes):
            blocks.append(
                types.BlobResourceContents(
                    uri=uri,
                    blob=base64.b64encode(item.content).decode("ascii"),
                    mime_type=item.mime_type or "application/octet-stream",
                )
            )
        else:
            blocks.append(
                types.TextResourceContents(
                    uri=uri,
                    text=item.content,
                    mime_type=item.mime_type or "text/plain",
                )
            )
    return types.ReadResourceResult(contents=blocks)


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


def register_file_resources(mcp: MCPServer) -> None:
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
        handler below, which can set the per-file MIME type a MCPServer
        template cannot; this body is the correct fallback if that
        override is ever absent.
        """
        contents, _metrics = read_ferumind_file_resource(
            f"{FILE_URI_PREFIX}{project}/{encoded_path}"
        )
        first = contents[0]
        return first.content

    async def handle_read_resource(
        _ctx: object,
        params: types.ReadResourceRequestParams,
    ) -> types.ReadResourceResult:
        """Serve one ``resources/read``, per-file MIME type included.

        Registered through the low-level server's public
        ``add_request_handler``, which replaces the handler ``MCPServer``
        installed for itself during construction. mcp 2.x removed the
        decorator form; the handler now takes ``(ctx, params)`` and returns a
        ``ReadResourceResult`` rather than an iterable of contents.

        Telemetry is not written here: ``CallObservationMiddleware`` observes
        ``resources/read`` with the same code that observes tool calls.
        """
        uri_text = str(params.uri)
        if not uri_text.startswith(FILE_URI_PREFIX):
            # Not ours: preserve whatever MCPServer would have served.
            contents = await mcp.read_resource(params.uri)
            if isinstance(contents, types.InputRequiredResult):  # pragma: no cover - unused
                raise _resource_error(
                    "UNSUPPORTED_RESOURCE",
                    "This resource requires client input, which Ferumind does not serve",
                )
            return _as_result(uri_text, contents)

        try:
            contents, _metrics = read_ferumind_file_resource(uri_text)
        except FerumindError as exc:
            raise _resource_error(exc.code, str(exc), exc.details) from exc
        except PathSafetyError as exc:
            # Never echo the resolved absolute path back to the client.
            raise _resource_error(
                "WORKSPACE_MISMATCH",
                "Resource path is outside the configured workspace boundary",
            ) from exc

        return _as_result(uri_text, contents)

    lowlevel_server(mcp).add_request_handler(
        "resources/read",
        types.ReadResourceRequestParams,
        handle_read_resource,
    )
