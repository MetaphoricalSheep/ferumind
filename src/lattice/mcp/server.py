"""Lattice MCP server — FastMCP assembly and entry point (spec-mcp v2).

Stateless per call: no sessions, no server-side conversation state. Every
project-scoped tool takes a required ``project`` argument; patch continuity
rides on ``operation_id`` + content hashes. Workspace-level compacts are a
separate tool family for explicit chat handoffs.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, cast

import anyio
import anyio.lowlevel
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio.to_thread import run_sync
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import Server
from mcp.shared.message import SessionMessage

from lattice.mcp.compact_tools import register_compact_tools
from lattice.mcp.file_tools import register_file_tools
from lattice.mcp.input_validation import apply_strict_input_validation
from lattice.mcp.observation import apply_observation_to_all_tools
from lattice.mcp.propose_tools import register_propose_tools
from lattice.mcp.read_tools import register_read_tools
from lattice.mcp.resources import register_file_resources
from lattice.mcp.tool_context import init_tool_context
from lattice.mcp.write_tools import register_write_tools

# The condensed bootstrap (spec-mcp §9) — exact string.
INSTRUCTIONS = (
    "Lattice is the user's shared Markdown workspace and the source of truth "
    "across chats. If the user explicitly invokes `/compact`, "
    "`@lattice /compact`, or asks for a Lattice compact, call "
    "`get_compact_instructions`. If the user invokes `/resume <token>` or asks "
    "to resume a Lattice compact, call `resume_compact`. For project work, "
    "call `get_context` with your project key before anything else, and obey "
    "the rules it returns. Never use compacts for ordinary project memory, "
    "notes, summaries, or document updates. Propose-then-apply for every edit; "
    "a propose result is not a saved edit. "
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

mcp = FastMCP(name="Lattice", instructions=INSTRUCTIONS)
_registered_mcp_id: int | None = None


def _suppress_sensitive_sdk_debug_logging() -> None:
    """Prevent the MCP SDK's full inbound-message debug log from exposing arguments."""

    sdk_logger = logging.getLogger("mcp.server.lowlevel.server")
    if sdk_logger.level == logging.NOTSET or sdk_logger.level < logging.INFO:
        sdk_logger.setLevel(logging.INFO)


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

    async def stdin_reader() -> None:
        try:
            async with read_stream_writer:
                while True:
                    raw_line = await run_sync(
                        stdin_buffer.readline,
                        MAX_STDIO_REQUEST_BYTES + 1,
                    )
                    if raw_line == b"":
                        break
                    if len(raw_line) > MAX_STDIO_REQUEST_BYTES:
                        await read_stream_writer.send(
                            ValueError(
                                f"MCP request exceeds the {MAX_STDIO_REQUEST_BYTES}-byte limit"
                            )
                        )
                        # Do not drain an attacker-controlled unterminated line:
                        # closing this transport is the only bounded recovery.
                        return
                    try:
                        line = raw_line.decode("utf-8")
                        message = types.JSONRPCMessage.model_validate_json(line)
                    except (UnicodeDecodeError, ValueError):
                        # Pydantic validation errors include input_value, which
                        # may contain document text or signed download URLs.
                        await read_stream_writer.send(ValueError("Malformed MCP JSON-RPC message"))
                        continue
                    await read_stream_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError:  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    async def stdout_writer() -> None:
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    json_text = session_message.message.model_dump_json(
                        by_alias=True,
                        exclude_none=True,
                    )
                    await run_sync(stdout.write, json_text + "\n")
                    await run_sync(stdout.flush)
        except anyio.ClosedResourceError:  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(stdin_reader)
        task_group.start_soon(stdout_writer)
        yield read_stream, write_stream


async def _run_stdio_server_async() -> None:
    _suppress_sensitive_sdk_debug_logging()
    # FastMCP exposes no public accessor for its low-level server, so private
    # access is required to drive the custom stdio transport below.
    lowlevel_server = cast(Server[object, object], mcp._mcp_server)  # pyright: ignore[reportPrivateUsage]
    async with _stdio_transport() as (read_stream, write_stream):
        await lowlevel_server.run(
            read_stream,
            write_stream,
            lowlevel_server.create_initialization_options(),
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
    register_write_tools(mcp)
    register_compact_tools(mcp)
    # Resources are registered alongside tools so ``resources/read`` is
    # available on the same surface that hands out the URIs.
    register_file_resources(mcp)
    apply_strict_input_validation(mcp)
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
    init_tool_context(workspace_path=workspace_path, transport=transport)
    register_all_tools()
    apply_observation_to_all_tools(mcp)
    _run_stdio_server()
