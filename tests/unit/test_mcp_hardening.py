"""Fail-closed MCP boundary behavior."""

from __future__ import annotations

import io
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import anyio
import pytest
from mcp.types import CallToolResult

from lattice.core.paths import WorkspaceRoot
from lattice.core.writes import ChatGPTSingleUploadResult
from lattice.mcp import observation, tool_context, write_tools
from lattice.mcp import server as mcp_server
from lattice.mcp.models import make_success
from lattice.mcp.observation import observe_tool
from lattice.mcp.server import mcp, register_all_tools, serve


async def _await[T](awaitable: Awaitable[T]) -> T:
    return await awaitable


def test_direct_network_transports_are_disabled() -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        serve(transport="streamable-http")
    with pytest.raises(RuntimeError, match="disabled"):
        serve(transport="sse")


def test_unhandled_tool_error_is_generic_and_does_not_log_secret(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def no_record(**_kwargs: object) -> None:
        return None

    def explode() -> CallToolResult:
        raise RuntimeError("signed-url-secret-value")

    monkeypatch.setattr(observation, "_record", no_record)
    caplog.set_level(logging.ERROR)
    result = observe_tool("explode", explode)()

    assert isinstance(result, CallToolResult)
    assert result.structuredContent is not None
    assert result.structuredContent["error_code"] == "INTERNAL_ERROR"
    assert "signed-url-secret-value" not in str(result.structuredContent)
    assert "signed-url-secret-value" not in caplog.text


def test_sdk_inbound_message_debug_logging_is_suppressed() -> None:
    logger = logging.getLogger("mcp.server.lowlevel.server")
    previous_level = logger.level
    try:
        logger.setLevel(logging.DEBUG)
        # This deliberately exercises Lattice's private SDK-boundary hardener.
        mcp_server._suppress_sensitive_sdk_debug_logging()  # pyright: ignore[reportPrivateUsage]
        assert logger.level == logging.INFO
    finally:
        logger.setLevel(previous_level)


def test_async_observation_awaits_and_records_completed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, object]] = []

    def record(**kwargs: object) -> None:
        recorded.append(kwargs)

    async def succeed(*, project: str) -> CallToolResult:
        await anyio.sleep(0)
        return make_success({"finished": True}, project=project)

    monkeypatch.setattr(observation, "_record", record)
    wrapped = observe_tool("async_tool", succeed)
    result = anyio.run(_await, cast(Awaitable[CallToolResult], wrapped(project="demo")))

    assert result.structuredContent is not None
    assert result.structuredContent["ok"] is True
    assert len(recorded) == 1
    assert recorded[0]["ok"] is True
    assert recorded[0]["project"] == "demo"


def test_actual_fastmcp_boundary_forbids_extra_and_coerced_arguments() -> None:
    register_all_tools()

    async def call_invalid(name: str, arguments: dict[str, object]) -> CallToolResult:
        result = await mcp.call_tool(name, arguments)
        assert isinstance(result, CallToolResult)
        return result

    invalid_cases: tuple[tuple[str, dict[str, object]], ...] = (
        (
            "start_library_file_upload",
            {
                "project": "demo",
                "filename": "x.pdf",
                "total_size": 1,
                "total_chunks": 1,
                "secret_extra": "must-not-be-reflected",
            },
        ),
        (
            "start_library_file_upload",
            {
                "project": "demo",
                "filename": "x.pdf",
                "total_size": "must-not-be-reflected",
                "total_chunks": 1,
            },
        ),
        (
            "upload_library_files_from_chatgpt",
            {
                "project": "demo",
                "files": (
                    '[{"download_url":"https://must-not-be-reflected.example","file_id":"file_1"}]'
                ),
            },
        ),
    )
    for name, arguments in invalid_cases:
        result = anyio.run(call_invalid, name, arguments)
        assert result.structuredContent is not None
        assert result.structuredContent == {
            "ok": False,
            "error_code": "VALIDATION_ERROR",
            "message": "Tool arguments do not match the declared input schema",
        }
        assert "must-not-be-reflected" not in str(result)

    # FastMCP has no public registered-tool iterator; inspect its registry to
    # prove every advertised top-level schema is closed, not just this sample.
    registered = mcp._tool_manager._tools  # pyright: ignore[reportPrivateUsage]
    for name, tool in registered.items():
        assert tool.parameters["additionalProperties"] is False, name


def test_malformed_stdio_exception_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "signed-url-secret-value"
    payload = f'{{"jsonrpc":"2.0","params":{{"url":"{secret}"}} bad}}\n'.encode()
    stdin = io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    async def run_inline(
        func: Callable[..., object],
        *args: object,
        **_kwargs: object,
    ) -> object:
        return func(*args)

    # This sandbox drops event-loop wakeups from worker threads. Running the
    # already-buffered read inline keeps this a deterministic transport test.
    monkeypatch.setattr(mcp_server, "run_sync", run_inline)

    async def exercise_transport() -> Exception:
        # Direct transport coverage is intentional: this is the security
        # boundary that converts untrusted bytes into SDK messages.
        async with mcp_server._stdio_transport() as (  # pyright: ignore[reportPrivateUsage]
            read_stream,
            write_stream,
        ):
            error = await read_stream.receive()
            await write_stream.aclose()
            assert isinstance(error, Exception)
            return error

    error = anyio.run(exercise_transport)
    assert str(error) == "Malformed MCP JSON-RPC message"
    assert secret not in str(error)


def test_oversized_unterminated_pipe_closes_without_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r", encoding="utf-8")
    writer = os.fdopen(write_fd, "wb", buffering=0)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(mcp_server, "MAX_STDIO_REQUEST_BYTES", 8)
    writer.write(b"123456789")

    async def run_inline(
        func: Callable[..., object],
        *args: object,
        **_kwargs: object,
    ) -> object:
        return func(*args)

    # The pipe already contains enough bytes to satisfy the bounded read.
    monkeypatch.setattr(mcp_server, "run_sync", run_inline)

    async def exercise_transport() -> Exception:
        async with mcp_server._stdio_transport() as (  # pyright: ignore[reportPrivateUsage]
            read_stream,
            write_stream,
        ):
            with anyio.fail_after(0.5):
                error = await read_stream.receive()
            await write_stream.aclose()
            assert isinstance(error, Exception)
            return error

    try:
        started = time.monotonic()
        error = anyio.run(exercise_transport)
        elapsed = time.monotonic() - started
    finally:
        writer.close()
        stdin.close()

    assert "exceeds the 8-byte limit" in str(error)
    assert elapsed < 0.5


def test_slow_remote_upload_does_not_block_other_mcp_calls(
    monkeypatch: pytest.MonkeyPatch,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    from lattice.core import writes

    def slow_upload(*_args: object, **_kwargs: object) -> ChatGPTSingleUploadResult:
        return ChatGPTSingleUploadResult(
            file_id="file_1",
            filename="file.bin",
            operation_id="op_1",
            snapshot_id="snap_1",
            path="library/file.bin",
            metadata_path="library/file.json",
            size_bytes=1,
            sha256="0" * 64,
        )

    monkeypatch.setattr(writes, "upload_library_file_from_chatgpt", slow_upload)
    tool_context.reset_tool_context()
    tool_context.init_tool_context(Path(workspace))
    register_all_tools()

    async def scenario() -> float:
        remote_results: list[object] = []
        offload_started = anyio.Event()
        resume_offload = anyio.Event()

        async def controlled_offload(
            func: Callable[..., object],
            *args: object,
            limiter: anyio.CapacityLimiter,
            **_kwargs: object,
        ) -> object:
            assert limiter.total_tokens == 2
            offload_started.set()
            await resume_offload.wait()
            return func(*args)

        monkeypatch.setattr(write_tools, "run_sync", controlled_offload)

        async def call_remote() -> None:
            remote_results.append(
                await mcp.call_tool(
                    "upload_library_file_from_chatgpt",
                    {
                        "project": project,
                        "file": {
                            "download_url": "https://files.example.test/file",
                            "file_id": "file_1",
                        },
                        "filename": "file.bin",
                    },
                )
            )

        began = time.monotonic()
        elapsed = float("inf")
        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(call_remote)
                await offload_started.wait()
                with anyio.fail_after(0.5):
                    listed = await mcp.call_tool("list_projects", {})
                assert isinstance(listed, CallToolResult)
                assert listed.structuredContent is not None
                assert listed.structuredContent["ok"] is True
                elapsed = time.monotonic() - began
                resume_offload.set()
            assert remote_results
            return elapsed
        finally:
            resume_offload.set()

    try:
        elapsed = anyio.run(scenario)
    finally:
        tool_context.reset_tool_context()

    assert elapsed < 0.5
