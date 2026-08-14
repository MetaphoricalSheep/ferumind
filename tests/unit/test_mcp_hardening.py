"""Fail-closed MCP boundary behavior."""

from __future__ import annotations

import io
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import anyio
import pytest
from mcp.types import CallToolResult

from ferumind.core.paths import WorkspaceRoot
from ferumind.core.runtime_events import (
    InternalErrorEvent,
    MalformedRequestEvent,
    ProcessStartedEvent,
    ProcessStoppingEvent,
    RequestTooLargeEvent,
    RuntimeEvent,
    RuntimeEventQuery,
    TransportClosedEvent,
    read_runtime_events,
)
from ferumind.core.upload_writes import ChatGPTSingleUploadResult
from ferumind.mcp import server as mcp_server
from ferumind.mcp import tool_context, upload_tools
from ferumind.mcp.sdk_internals import registered_tools
from ferumind.mcp.server import mcp, register_all_tools, serve


def _runtime_event_recorder(
    events: list[RuntimeEvent],
) -> Callable[[Callable[[], RuntimeEvent]], None]:
    def record(event_factory: Callable[[], RuntimeEvent]) -> None:
        events.append(event_factory())

    return record


def test_direct_network_transports_are_disabled() -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        serve(transport="streamable-http")
    with pytest.raises(RuntimeError, match="disabled"):
        serve(transport="sse")


def test_unhandled_tool_error_is_generic_and_does_not_log_secret(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    workspace: WorkspaceRoot,
) -> None:
    """A crashing tool body must never put its exception text on the wire.

    Exercised through the SDK's real invocation path, not through a wrapper in
    isolation, because that path is where the danger is: ``Tool.run`` re-raises
    any exception as ``ToolError(f"Error executing tool {name}: {e}")`` and
    ``MCPServer._handle_call_tool`` returns that string as tool content. A bare
    mcp 2.0.0 server answers ``RuntimeError("SECRET")`` with ``…: SECRET``.
    Ferumind's tool boundary is the only thing preventing that.
    """
    secret = "signed-url-secret-value"
    tool_context.reset_tool_context()
    tool_context.init_tool_context(Path(workspace))
    register_all_tools()

    def explode() -> CallToolResult:
        raise RuntimeError(f"https://host/download?sig={secret}")

    tool = next(t for t in registered_tools(mcp) if t.name == "list_projects")
    monkeypatch.setattr(tool, "fn", explode)
    caplog.set_level(logging.ERROR)

    async def call() -> CallToolResult:
        result = await mcp.call_tool("list_projects", {})
        assert isinstance(result, CallToolResult)
        return result

    try:
        result = anyio.run(call)
    finally:
        tool_context.reset_tool_context()

    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["error_code"] == "INTERNAL_ERROR"
    # The whole result, not just the envelope: content blocks travel too.
    assert secret not in str(result)
    assert secret not in caplog.text
    details = cast("dict[str, object]", result.structured_content["details"])
    correlation_id = details["correlation_id"]
    assert isinstance(correlation_id, str)
    runtime = read_runtime_events(workspace, RuntimeEventQuery(correlation_id=correlation_id))
    assert len(runtime.events) == 1
    event = runtime.events[0]
    assert isinstance(event, InternalErrorEvent)
    assert event.frames
    assert event.exception_type.endswith("RuntimeError")
    assert secret not in event.model_dump_json()


def test_sdk_inbound_message_debug_logging_is_suppressed() -> None:
    """The SDK's inbound-message dump carries tool arguments and patch bodies.

    Pinned by ``configure_logging`` rather than by a bespoke helper: the pin has
    to survive a later ``FERUMIND_LOG_LEVEL=DEBUG``, and a one-shot call at
    startup would not. The logger name is unchanged in mcp 2.x.
    """
    from ferumind.core.logging_setup import PINNED_LOGGERS, configure_logging

    assert PINNED_LOGGERS["mcp.server.lowlevel.server"] == logging.INFO
    logger = logging.getLogger("mcp.server.lowlevel.server")
    previous_level = logger.level
    try:
        logger.setLevel(logging.DEBUG)
        configure_logging("DEBUG")
        assert logger.level == logging.INFO
        assert not logger.isEnabledFor(logging.DEBUG)
    finally:
        logger.setLevel(previous_level)


def test_caught_unexpected_tool_error_also_gets_a_correlated_runtime_event(
    workspace: WorkspaceRoot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "caught-error-secret-canary"
    tool_context.reset_tool_context()
    tool_context.init_tool_context(Path(workspace))
    caplog.set_level(logging.ERROR)
    try:
        result = tool_context.error_result(RuntimeError(secret), project="demo")
    finally:
        tool_context.reset_tool_context()

    assert result.structured_content is not None
    details = cast("dict[str, object]", result.structured_content["details"])
    correlation_id = details["correlation_id"]
    assert isinstance(correlation_id, str)
    events = read_runtime_events(workspace, RuntimeEventQuery(correlation_id=correlation_id)).events
    assert len(events) == 1
    assert isinstance(events[0], InternalErrorEvent)
    assert secret not in events[0].model_dump_json()
    assert secret not in caplog.text


def test_actual_sdk_boundary_forbids_extra_and_coerced_arguments() -> None:
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
        assert result.structured_content is not None
        assert result.structured_content == {
            "ok": False,
            "error_code": "VALIDATION_ERROR",
            "message": "Tool arguments do not match the declared input schema",
        }
        assert "must-not-be-reflected" not in str(result)

    # Prove every advertised top-level schema is closed, not just this sample.
    for tool in registered_tools(mcp):
        assert tool.parameters["additionalProperties"] is False, tool.name


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
    events: list[RuntimeEvent] = []
    monkeypatch.setattr(mcp_server, "_try_runtime_event", _runtime_event_recorder(events))

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
    assert any(isinstance(event, MalformedRequestEvent) for event in events)
    assert any(isinstance(event, TransportClosedEvent) for event in events)
    assert secret not in "".join(event.model_dump_json() for event in events)


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
    events: list[RuntimeEvent] = []
    monkeypatch.setattr(mcp_server, "_try_runtime_event", _runtime_event_recorder(events))

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
    assert any(isinstance(event, RequestTooLargeEvent) for event in events)
    close = next(event for event in events if isinstance(event, TransportClosedEvent))
    assert close.reason == "request_too_large"


def test_serve_records_process_start_and_clean_stop(
    monkeypatch: pytest.MonkeyPatch,
    workspace: WorkspaceRoot,
) -> None:
    events: list[RuntimeEvent] = []
    monkeypatch.setattr(mcp_server, "_try_runtime_event", _runtime_event_recorder(events))
    monkeypatch.setattr(mcp_server, "_run_stdio_server", lambda: None)

    try:
        serve(workspace_path=Path(workspace))
    finally:
        tool_context.reset_tool_context()

    assert [event.event for event in events] == ["process_started", "process_stopping"]
    assert isinstance(events[0], ProcessStartedEvent)
    assert isinstance(events[1], ProcessStoppingEvent)
    assert events[1].reason == "normal"


def test_serve_records_keyboard_interrupt_as_a_clean_stop(
    monkeypatch: pytest.MonkeyPatch,
    workspace: WorkspaceRoot,
) -> None:
    events: list[RuntimeEvent] = []
    monkeypatch.setattr(mcp_server, "_try_runtime_event", _runtime_event_recorder(events))

    def interrupt() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(mcp_server, "_run_stdio_server", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            serve(workspace_path=Path(workspace))
    finally:
        tool_context.reset_tool_context()

    stop = events[-1]
    assert isinstance(stop, ProcessStoppingEvent)
    assert stop.reason == "keyboard_interrupt"


def test_unexpected_server_exit_does_not_claim_a_clean_stop(
    monkeypatch: pytest.MonkeyPatch,
    workspace: WorkspaceRoot,
) -> None:
    events: list[RuntimeEvent] = []
    monkeypatch.setattr(mcp_server, "_try_runtime_event", _runtime_event_recorder(events))

    def crash() -> None:
        raise RuntimeError("server-secret-canary")

    monkeypatch.setattr(mcp_server, "_run_stdio_server", crash)
    try:
        with pytest.raises(RuntimeError, match="server-secret-canary"):
            serve(workspace_path=Path(workspace))
    finally:
        tool_context.reset_tool_context()

    assert [event.event for event in events] == ["process_started"]


def test_runtime_event_construction_failure_is_non_interfering(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "runtime-event-construction-secret-canary"

    def fail_to_construct() -> RuntimeEvent:
        raise RuntimeError(secret)

    caplog.set_level(logging.ERROR)
    mcp_server._try_runtime_event(fail_to_construct)  # pyright: ignore[reportPrivateUsage]

    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text


def test_slow_remote_upload_does_not_block_other_mcp_calls(
    monkeypatch: pytest.MonkeyPatch,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    from ferumind.core import upload_writes

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

    monkeypatch.setattr(upload_writes, "upload_library_file_from_chatgpt", slow_upload)
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

        monkeypatch.setattr(upload_tools, "run_sync", controlled_offload)

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
                assert listed.structured_content is not None
                assert listed.structured_content["ok"] is True
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


# ── Private SDK internals must fail closed (REL-021 part A) ─────────────────


class TestPrivateSdkHooksFailClosed:
    """The two surviving private SDK attachment points must fail loudly.

    mcp 2.x made observation and the server version public constructor
    parameters, so those can no longer silently fail to attach. What remains is
    ``_lowlevel_server`` (the bounded stdio transport and the resources/read
    handler) and ``_tool_manager`` (to reach its public accessors, and to
    replace generated argument metadata). A rename in either must abort startup
    rather than serve without argument validation and error sanitisation.
    """

    class _RenamedAway:
        """An MCPServer whose private attachment points have been renamed."""

    class _ManagerWithoutListTools:
        class _Manager:
            pass

        _tool_manager = _Manager()

    def test_lowlevel_server_access_fails_closed(self) -> None:
        from ferumind.mcp.sdk_internals import lowlevel_server

        with pytest.raises(RuntimeError, match="low-level server is unavailable"):
            lowlevel_server(self._RenamedAway())

    def test_tool_manager_access_fails_closed(self) -> None:
        from ferumind.mcp.sdk_internals import registered_tools

        with pytest.raises(RuntimeError, match="tool manager is unavailable"):
            registered_tools(self._RenamedAway())

    def test_tool_manager_without_public_accessor_fails_closed(self) -> None:
        from ferumind.mcp.sdk_internals import registered_tools

        with pytest.raises(RuntimeError, match="no list_tools"):
            registered_tools(self._ManagerWithoutListTools())

    def test_tool_boundary_cannot_be_skipped_silently(self) -> None:
        """Regression: a renamed hook must not degrade the server quietly.

        Both accessors raise rather than return ``None``/empty, so an
        unvalidated, unsanitised server cannot start.
        """
        import inspect

        from ferumind.mcp import sdk_internals

        source = inspect.getsource(sdk_internals)
        assert source.count("raise RuntimeError") == 3
        assert "logger.warning" not in source, (
            "a warning is not enough: a server missing these hooks must not start"
        )

    def test_observation_is_installed_by_construction(self) -> None:
        """Observation is a constructor argument, so it cannot fail to attach.

        This is what replaced the old fail-closed guard: there is no attachment
        step left to fail.
        """
        from ferumind.mcp.observation import CallObservationMiddleware, LifecycleEventMiddleware
        from ferumind.mcp.sdk_internals import lowlevel_server

        installed = lowlevel_server(mcp).middleware
        assert any(isinstance(m, CallObservationMiddleware) for m in installed), (
            f"call observation is not installed; middleware = {installed!r}"
        )
        assert any(isinstance(m, LifecycleEventMiddleware) for m in installed), (
            f"initialize lifecycle observation is not installed; middleware = {installed!r}"
        )


def test_mcp_sdk_range_is_capped_to_the_tested_major() -> None:
    """An uncapped range would admit an untested major.

    Two private SDK attachment points survive the 2.x migration
    (``_lowlevel_server`` and ``_tool_manager``), so a major bump can still
    break startup or the tool boundary. The declared range must match what the
    surface suite has actually run against.
    """
    import tomllib

    repo_root = Path(__file__).resolve().parent.parent.parent
    with (repo_root / "pyproject.toml").open("rb") as handle:
        deps = tomllib.load(handle)["project"]["dependencies"]

    mcp_specs = [dep for dep in deps if re.match(r"^mcp(\[|[<>=!~]|$)", dep)]
    assert len(mcp_specs) == 1, mcp_specs
    spec = mcp_specs[0]
    assert "[" not in spec, (
        f"mcp is declared as {spec!r}. Its only extra is `cli`, which adds "
        "typer (already a direct dependency) and python-dotenv (unused). "
        "Declaring it pulls a runtime dependency nothing imports."
    )
    assert ">=2.0.0" in spec, (
        f"mcp is declared as {spec!r}. 1.x is upstream security-only "
        "maintenance and Ferumind migrated off it (REL-035)."
    )
    assert "<3" in spec, (
        f"mcp is declared as {spec!r}. It must stay capped below the first "
        "untested major while private SDK attachment points are in use."
    )


def test_installed_mcp_sdk_matches_the_declared_range() -> None:
    """Assert the SDK version from inside the process, not from an installer.

    A version matrix that trusts its own setup can silently test the same
    version twice: ``uv sync`` will reinstall the locked version over a pinned
    one, and ``uv pip install`` targets ``VIRTUAL_ENV`` rather than
    ``UV_PROJECT_ENVIRONMENT``. Only an in-process check proves which SDK ran.

    So the CI compatibility matrix sets ``FERUMIND_EXPECTED_MCP_VERSION`` to the
    version that row is *supposed* to exercise, and this test refuses to pass on
    anything else. That turns a silent reinstall from a green row into a named
    failure. The variable is a CI job parameter, not Ferumind configuration: it
    is set per matrix row in ``.github/workflows/ci.yml`` and belongs in no
    ``Config`` model. Unset — every ordinary ``just verify`` — the major check
    below is the whole test, so a normal run is unaffected.
    """
    from importlib.metadata import version as pkg_version

    installed = pkg_version("mcp")
    expected = os.environ.get("FERUMIND_EXPECTED_MCP_VERSION")
    if expected:
        assert installed == expected, (
            f"this matrix row was told to exercise MCP SDK {expected} but is "
            f"running against {installed}. The environment was not built the way "
            "the job intended: `uv sync`/`uv run` reinstall the locked version "
            "over a pinned one, and `uv pip install` targets VIRTUAL_ENV rather "
            "than UV_PROJECT_ENVIRONMENT. Fix the install step — do not relax "
            "this assertion, or the row reports green for a version it never ran."
        )

    major = int(installed.split(".", 1)[0])
    assert major == 2, (
        f"the MCP SDK in this environment is {installed}, outside the "
        "declared >=2.0.0,<3 range that the surface suite has been run against"
    )
