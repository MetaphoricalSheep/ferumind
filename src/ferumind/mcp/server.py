"""Ferumind MCP server — MCPServer assembly and entry point (spec-mcp).

Stateless per call: no sessions, no server-side conversation state. Every
project-scoped tool takes a required ``project`` argument; patch continuity
rides on ``operation_id`` + content hashes. Workspace-level compacts are a
separate tool family for explicit chat handoffs.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, TextIO

import anyio
import anyio.lowlevel
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio.to_thread import run_sync
from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.shared.message import SessionMessage

from ferumind.core.config import load_config
from ferumind.core.logging_setup import configure_logging
from ferumind.core.runtime_events import (
    MalformedRequestEvent,
    ProcessStartedEvent,
    ProcessStoppingEvent,
    RequestTooLargeEvent,
    RuntimeEvent,
    TransportClosedEvent,
    TransportCloseReason,
    exception_type_name,
    try_append_runtime_event,
)
from ferumind.core.version import package_version
from ferumind.mcp.compact_tools import register_compact_tools
from ferumind.mcp.document_tools import register_document_tools
from ferumind.mcp.file_tools import register_file_tools
from ferumind.mcp.lifecycle_tools import register_lifecycle_tools
from ferumind.mcp.observation import CallObservationMiddleware, LifecycleEventMiddleware
from ferumind.mcp.project_tools import register_project_tools
from ferumind.mcp.propose_tools import register_propose_tools
from ferumind.mcp.read_tools import register_read_tools
from ferumind.mcp.resources import register_file_resources
from ferumind.mcp.sdk_internals import lowlevel_server
from ferumind.mcp.tool_boundary import apply_tool_boundary
from ferumind.mcp.tool_context import init_tool_context, require_workspace
from ferumind.mcp.upload_tools import register_upload_tools

# The condensed bootstrap (spec-mcp §9) — exact string.
INSTRUCTIONS = (
    "Ferumind is the user's shared Markdown workspace and the source of truth "
    "across chats. If the user explicitly invokes `/compact`, "
    "`@ferumind /compact`, or asks for a Ferumind compact, call "
    "`get_compact_instructions`. If the user invokes `/resume <token>` or asks "
    "to resume a Ferumind compact, call `resume_compact`. For project work, "
    "call `get_context` with your project key before anything else, and obey "
    "the rules it returns. Never use compacts for ordinary project memory, "
    "notes, summaries, or document updates. Propose-then-apply for every edit; "
    "a propose result is not a saved edit. "
    "Every tool answers with the same envelope: `ok` true means read `data`, "
    "`ok` false means read `error_code`. Each `propose_*` call stages a pending "
    "edit and writes nothing — save it with `apply_patch`, drop it with "
    "`discard_patch`, and note that it expires after 24 hours. "
    "A project also holds non-Markdown files (photographs, PDFs, exports). "
    "They are not in get_context and not searchable by content. To work with "
    "one: read the project's rules, spine, and documents first — they carry "
    "the workspace's own conventions and often reference files by path; call "
    "`list_files` when you do not already know the path; call `read_file` to "
    "put a supported representation (image rendition or bounded text) into "
    "context; and use the `resource_uri` it returns when you need the exact "
    "original. There is no required folder for files, and a file's meaning "
    "never follows from its folder, filename, or extension alone — read the "
    "documents that reference it."
)

MAX_STDIO_REQUEST_BYTES = 32 * 1024 * 1024
logger = logging.getLogger(__name__)


def _safe_error_log(message: str, *args: object) -> None:
    """Keep runtime-event logging failures outside protocol transport work."""

    try:
        logger.error(message, *args)
    except Exception:  # Logging is observability and must not alter transport work.
        return


def _package_version() -> str:
    """Ferumind's own version for the MCP ``serverInfo`` block.

    An unversioned server reports an empty string, and on the 1.x SDK it
    reported ``pkg_version("mcp")`` instead — so Ferumind used to advertise the
    *SDK's* version as its own, and the number moved on every SDK upgrade.

    Delegates to ``core.version`` so the CLI and this block cannot report
    different numbers.
    """
    return package_version()


# ``version`` and ``middleware`` are public constructor parameters on mcp 2.x.
# Both used to require reaching past the public API: the version was assigned
# onto the private low-level server after construction, and observation was
# monkey-patched onto every registered tool's ``fn``. Declaring them here means
# a server cannot exist unversioned or unobserved.
mcp = MCPServer(
    name="Ferumind",
    instructions=INSTRUCTIONS,
    version=_package_version(),
    middleware=[CallObservationMiddleware(), LifecycleEventMiddleware()],
)
_registered_mcp_id: int | None = None


def _try_runtime_event(event_factory: Callable[[], RuntimeEvent]) -> None:
    """Build and persist runtime metadata without affecting the server."""

    try:
        event = event_factory()
        try_append_runtime_event(require_workspace(), event)
    except Exception as exc:
        _safe_error_log(
            "Failed to prepare a private runtime event (type=%s)",
            type(exc).__name__,
        )


def _malformed_request_event_factory(exc: BaseException) -> Callable[[], RuntimeEvent]:
    """Bind an exception for safe, immediate construction inside containment."""

    def build() -> RuntimeEvent:
        return MalformedRequestEvent(exception_type=exception_type_name(exc))

    return build


@dataclass
class _TransportRuntimeState:
    """Ensure one close event is emitted for the stdio transport."""

    close_reason: TransportCloseReason | None = None

    def record_close(self, reason: TransportCloseReason) -> None:
        if self.close_reason is not None:
            return
        self.close_reason = reason
        _try_runtime_event(lambda: TransportClosedEvent(reason=reason))


async def _stdin_reader(
    writer: MemoryObjectSendStream[SessionMessage | Exception],
    stdin_buffer: BinaryIO,
    state: _TransportRuntimeState,
) -> None:
    try:
        async with writer:
            while True:
                raw_line = await run_sync(
                    stdin_buffer.readline,
                    MAX_STDIO_REQUEST_BYTES + 1,
                )
                if raw_line == b"":
                    state.record_close("eof")
                    break
                if len(raw_line) > MAX_STDIO_REQUEST_BYTES:
                    _record_oversized_request(state, len(raw_line))
                    await writer.send(
                        ValueError(f"MCP request exceeds the {MAX_STDIO_REQUEST_BYTES}-byte limit")
                    )
                    # Do not drain an attacker-controlled unterminated line:
                    # closing this transport is the only bounded recovery.
                    return
                await _decode_and_send(raw_line, writer)
    except anyio.ClosedResourceError:  # pragma: no cover
        await anyio.lowlevel.checkpoint()


def _record_oversized_request(state: _TransportRuntimeState, received_bytes: int) -> None:
    _try_runtime_event(
        lambda: RequestTooLargeEvent(
            limit_bytes=MAX_STDIO_REQUEST_BYTES,
            received_at_least_bytes=received_bytes,
        )
    )
    state.record_close("request_too_large")


async def _decode_and_send(
    raw_line: bytes,
    writer: MemoryObjectSendStream[SessionMessage | Exception],
) -> None:
    try:
        line = raw_line.decode("utf-8")
        # mcp 2.x: the JSON-RPC message unions are plain unions, not
        # ``RootModel``s, so validation goes through the SDK adapter.
        message = types.jsonrpc_message_adapter.validate_json(line)
    except (UnicodeDecodeError, ValueError) as exc:
        # Validation errors include input_value, which may contain document
        # text or signed URLs. Persist only the exception type.
        _try_runtime_event(_malformed_request_event_factory(exc))
        await writer.send(ValueError("Malformed MCP JSON-RPC message"))
        return
    await writer.send(SessionMessage(message))


async def _stdout_writer(
    reader: MemoryObjectReceiveStream[SessionMessage],
    stdout: TextIO,
) -> None:
    try:
        async with reader:
            async for session_message in reader:
                json_text = session_message.message.model_dump_json(
                    by_alias=True,
                    exclude_none=True,
                )
                await run_sync(stdout.write, json_text + "\n")
                await run_sync(stdout.flush)
    except anyio.ClosedResourceError:  # pragma: no cover
        await anyio.lowlevel.checkpoint()


@asynccontextmanager
async def _stdio_transport() -> AsyncGenerator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """Provide a stdio transport that works reliably with subprocess pipes."""

    read_stream_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](
        0
    )
    write_stream, write_stream_reader = anyio.create_memory_object_stream[SessionMessage](0)
    stdin_buffer = sys.stdin.buffer
    stdout = sys.stdout
    state = _TransportRuntimeState()

    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_stdin_reader, read_stream_writer, stdin_buffer, state)
            task_group.start_soon(_stdout_writer, write_stream_reader, stdout)
            yield read_stream, write_stream
    finally:
        state.record_close("server_stopping")


async def _run_stdio_server_async() -> None:
    # MCPServer exposes no public accessor for its low-level server, and
    # ``run_stdio_async()`` owns its own transport — it cannot be given the
    # bounded, redacting streams above. This is one of the two surviving
    # private attachment points; the SDK range in pyproject.toml is capped
    # because of them.
    lowlevel = lowlevel_server(mcp)
    async with _stdio_transport() as (read_stream, write_stream):
        await lowlevel.run(
            read_stream,
            write_stream,
            lowlevel.create_initialization_options(),
        )


def _run_stdio_server() -> None:
    anyio.run(_run_stdio_server_async)


def register_all_tools() -> None:
    """Register all MCP tools and resource handling (idempotent)."""
    global _registered_mcp_id
    if _registered_mcp_id == id(mcp):
        return
    register_read_tools(mcp)
    register_file_tools(mcp)
    register_propose_tools(mcp)
    register_document_tools(mcp)
    register_upload_tools(mcp)
    register_lifecycle_tools(mcp)
    register_project_tools(mcp)
    register_compact_tools(mcp)
    # Resources are registered alongside tools so ``resources/read`` is
    # available on the same surface that hands out the URIs.
    register_file_resources(mcp)
    apply_tool_boundary(mcp)
    _registered_mcp_id = id(mcp)


def serve(
    workspace_path: Path | None = None,
    transport: Literal["stdio", "sse", "streamable-http"] = "stdio",
) -> None:
    """Initialize and serve the local stdio MCP server.

    Network transports remain deliberately disabled until server-verified
    OAuth, authorization, rate limiting, and deployment controls ship.
    """
    if transport != "stdio":
        raise RuntimeError(
            "Direct MCP network transports are disabled until authenticated "
            "remote serving is implemented"
        )
    # Idempotent: a no-op when the CLI callback already configured logging, and
    # the safety pins are re-applied either way. Serving through an embedder
    # that bypasses the CLI still gets stderr-only, credential-safe logging.
    configure_logging(load_config(workspace_path).log_level)
    init_tool_context(workspace_path=workspace_path, transport=transport)
    register_all_tools()
    _try_runtime_event(
        lambda: ProcessStartedEvent(
            transport=transport,
            package_version=_package_version(),
        )
    )
    try:
        _run_stdio_server()
    except KeyboardInterrupt:
        _try_runtime_event(lambda: ProcessStoppingEvent(reason="keyboard_interrupt"))
        raise
    else:
        _try_runtime_event(lambda: ProcessStoppingEvent(reason="normal"))
