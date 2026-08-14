"""Behaviour of the call-observation middleware (spec-mcp §8).

The middleware replaced a wrapper that monkey-patched every registered tool's
``fn``. These tests pin the properties that move changed: what is observed, what
is deliberately not, and what must never enter the log.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import anyio
import pytest

from ferumind.core.file_uri import build_file_uri
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.runtime_events import (
    ClientInitializedEvent,
    ObservationWriteFailedEvent,
    RuntimeEvent,
)
from ferumind.mcp import observation as observation_module
from ferumind.mcp.observation import (
    MAX_ARGUMENT_KEY_CHARS,
    MAX_ARGUMENT_KEYS,
    CallObservationMiddleware,
    LifecycleEventMiddleware,
    current_correlation_id,
)


@dataclass
class _ClientInfo:
    name: str = "probe-client"
    version: str = "9.9.9"


@dataclass
class _ClientParams:
    client_info: _ClientInfo = field(default_factory=_ClientInfo)


@dataclass
class _Session:
    client_params: _ClientParams = field(default_factory=_ClientParams)


@dataclass
class _Ctx:
    """The subset of ``ServerRequestContext`` the middleware reads."""

    method: str
    params: dict[str, Any] | None = None
    protocol_version: str = "2026-07-28"
    session: _Session = field(default_factory=_Session)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture observation rows without touching a database."""
    rows: list[dict[str, Any]] = []

    def fake_record(write: object) -> None:
        values = vars(write).copy()
        call = cast("dict[str, Any]", vars(values.pop("call")))
        values.update(call)
        values["argument_keys"] = list(values["argument_keys"])
        rows.append(values)

    monkeypatch.setattr("ferumind.mcp.observation._record", fake_record)
    return rows


def _run(ctx: _Ctx, result: Any = None) -> Any:
    middleware = CallObservationMiddleware()

    async def call_next(_ctx: Any) -> Any:
        return result

    async def main() -> Any:
        return await middleware(ctx, call_next)

    return anyio.run(main)


class TestWhatIsObserved:
    def test_tool_calls_are_recorded(self, recorded: list[dict[str, Any]]) -> None:
        ctx = _Ctx("tools/call", {"name": "get_context", "arguments": {"project": "demo"}})
        _run(ctx, {"structuredContent": {"ok": True}, "isError": False})

        assert len(recorded) == 1
        row = recorded[0]
        assert row["tool_name"] == "get_context"
        assert row["project"] == "demo"
        assert row["ok"] is True
        assert row["argument_keys"] == ["project"]

    def test_get_context_records_every_payload_metric(self, recorded: list[dict[str, Any]]) -> None:
        _run(
            _Ctx("tools/call", {"name": "get_context", "arguments": {"project": "demo"}}),
            {
                "structuredContent": {
                    "ok": True,
                    "data": {
                        "payload": {
                            "rules_bytes": 100,
                            "spine_bytes": 50,
                            "documents_count": 3,
                            "descriptions_bytes": 75,
                        }
                    },
                },
                "isError": False,
            },
        )

        assert recorded[0]["context_metrics"] == {
            "rules_bytes": 100,
            "spine_bytes": 50,
            "documents_count": 3,
            "descriptions_bytes": 75,
        }

    def test_resource_reads_are_recorded_by_the_same_code(
        self, recorded: list[dict[str, Any]]
    ) -> None:
        """Previously a hand-written copy of this bookkeeping lived in
        ``resources.py``; the middleware covers both methods now.
        """
        uri = build_file_uri("demo", "library/report.pdf")
        ctx = _Ctx("resources/read", {"uri": uri})
        _run(
            ctx,
            {"contents": [{"uri": "x", "blob": "AAAA", "mimeType": "application/pdf"}]},
        )

        assert len(recorded) == 1
        row = recorded[0]
        assert row["tool_name"] == "resources/read"
        assert row["project"] == "demo"
        assert row["argument_keys"] == ["uri"]
        assert row["context_metrics"] == {
            "mime_type": "application/pdf",
            "kind": "blob",
            "size_bytes": 3,
        }

    @pytest.mark.parametrize("method", ["initialize", "tools/list", "resources/templates/list"])
    def test_other_methods_pass_through_unrecorded(
        self, recorded: list[dict[str, Any]], method: str
    ) -> None:
        sentinel = {"untouched": True}
        assert _run(_Ctx(method), sentinel) is sentinel
        assert recorded == []

    def test_argument_validation_failures_are_now_visible(
        self, recorded: list[dict[str, Any]]
    ) -> None:
        """New behaviour. The old wrapper sat on the tool function, which a
        rejected call never reached, so bad-input attempts left no trace.
        """
        ctx = _Ctx("tools/call", {"name": "list_projects", "arguments": {"bogus": 1}})
        _run(
            ctx,
            {
                "structuredContent": {"ok": False, "error_code": "VALIDATION_ERROR"},
                "isError": True,
            },
        )

        assert recorded[0]["ok"] is False
        assert recorded[0]["error_code"] == "VALIDATION_ERROR"
        assert recorded[0]["argument_keys"] == ["bogus"]


class TestClientIdentity:
    def test_identity_is_recorded_when_the_transport_exposes_it(
        self, recorded: list[dict[str, Any]]
    ) -> None:
        _run(_Ctx("tools/call", {"name": "t", "arguments": {}}), {"isError": False})

        row = recorded[0]
        assert row["client_name"] == "probe-client"
        assert row["client_version"] == "9.9.9"
        assert row["protocol_version"] == "2026-07-28"

    def test_absent_identity_stays_null_and_is_never_guessed(
        self, recorded: list[dict[str, Any]]
    ) -> None:
        ctx = _Ctx("tools/call", {"name": "t", "arguments": {}})
        ctx.session = _Session(client_params=None)  # type: ignore[arg-type]  # absent on some transports
        _run(ctx, {"isError": False})

        row = recorded[0]
        assert row["client_name"] is None
        assert row["client_version"] is None


class TestHostileInput:
    def test_argument_key_names_are_bounded(self, recorded: list[dict[str, Any]]) -> None:
        """Keys are read before validation, so their names are caller-supplied."""
        arguments = {f"k{index:04d}": 1 for index in range(MAX_ARGUMENT_KEYS * 4)}
        arguments["x" * 500] = 1
        _run(
            _Ctx("tools/call", {"name": "t", "arguments": arguments}),
            {"isError": False},
        )

        keys = recorded[0]["argument_keys"]
        assert len(keys) <= MAX_ARGUMENT_KEYS
        assert all(len(key) <= MAX_ARGUMENT_KEY_CHARS for key in keys)

    def test_argument_values_never_enter_the_log(self, recorded: list[dict[str, Any]]) -> None:
        secret = "sk-live-must-not-be-recorded"
        _run(
            _Ctx("tools/call", {"name": "t", "arguments": {"token": secret, "project": "demo"}}),
            {"structuredContent": {"ok": True}, "isError": False},
        )

        assert secret not in repr(recorded[0])

    def test_a_malformed_project_argument_is_not_recorded_as_a_project(
        self, recorded: list[dict[str, Any]]
    ) -> None:
        _run(
            _Ctx("tools/call", {"name": "t", "arguments": {"project": {"nested": "object"}}}),
            {"isError": False},
        )
        assert recorded[0]["project"] is None

    def test_an_unparseable_resource_uri_records_a_null_project(
        self, recorded: list[dict[str, Any]]
    ) -> None:
        _run(_Ctx("resources/read", {"uri": "ferumind://file/%%%bad"}), {"contents": []})
        assert recorded[0]["project"] is None


class TestFailurePaths:
    def test_a_raised_error_is_recorded_then_re_raised(
        self, recorded: list[dict[str, Any]]
    ) -> None:
        """Protocol-level failures must still reach the client unchanged."""
        middleware = CallObservationMiddleware()

        async def call_next(_ctx: Any) -> Any:
            raise RuntimeError("boom")

        async def main() -> None:
            await middleware(_Ctx("tools/call", {"name": "t", "arguments": {}}), call_next)

        with pytest.raises(RuntimeError, match="boom"):
            anyio.run(main)

        assert recorded[0]["ok"] is False
        assert recorded[0]["error_code"] == "RuntimeError"

    def test_observation_failure_never_breaks_the_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        workspace: WorkspaceRoot,
    ) -> None:
        calls: list[str] = []
        events: list[RuntimeEvent] = []

        def exploding_record(**_kwargs: Any) -> None:
            calls.append("require_database")
            raise sqlite_error()

        def sqlite_error() -> Exception:
            return RuntimeError("database is locked")

        def capture_event(_workspace: WorkspaceRoot, event: RuntimeEvent) -> bool:
            events.append(event)
            return True

        monkeypatch.setattr("ferumind.mcp.observation.require_database", exploding_record)
        monkeypatch.setattr(observation_module, "require_workspace", lambda: workspace)
        monkeypatch.setattr(observation_module, "try_append_runtime_event", capture_event)
        result = _run(
            _Ctx("tools/call", {"name": "t", "arguments": {}}),
            {"structuredContent": {"ok": True}, "isError": False},
        )
        # The result below is also what a working observation layer returns, so
        # the recorder is what keeps this test about the failure it names.
        assert calls == ["require_database"], "the injected observation failure never fired"
        assert result == {"structuredContent": {"ok": True}, "isError": False}
        assert len(events) == 1
        assert isinstance(events[0], ObservationWriteFailedEvent)

    def test_metric_extractor_failure_preserves_the_successful_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
        workspace: WorkspaceRoot,
    ) -> None:
        calls: list[str] = []
        events: list[RuntimeEvent] = []
        rows: list[object] = []

        def explode(_structured: object) -> None:
            calls.append("metric_extraction")
            raise RuntimeError("signed-url-secret-canary")

        def capture_event(_workspace: WorkspaceRoot, event: RuntimeEvent) -> bool:
            events.append(event)
            return True

        extractors = cast(
            "dict[str, Callable[[object], object]]",
            vars(observation_module)["_METRIC_EXTRACTORS"],
        )
        monkeypatch.setitem(extractors, "adversarial_metric", explode)
        monkeypatch.setattr(observation_module, "_record", rows.append)
        monkeypatch.setattr(observation_module, "require_workspace", lambda: workspace)
        monkeypatch.setattr(observation_module, "try_append_runtime_event", capture_event)
        sentinel = {"structuredContent": {"ok": True}, "isError": False}

        result = _run(
            _Ctx("tools/call", {"name": "adversarial_metric", "arguments": {}}),
            sentinel,
        )

        assert result is sentinel
        assert calls == ["metric_extraction"], "the injected metric failure never fired"
        assert len(rows) == 1
        assert len(events) == 1
        failure = events[0]
        assert isinstance(failure, ObservationWriteFailedEvent)
        assert failure.stage == "metric_extraction"
        assert "signed-url-secret-canary" not in failure.model_dump_json()

    def test_observation_db_failure_writes_a_correlated_safe_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        workspace: WorkspaceRoot,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        calls: list[str] = []
        events: list[RuntimeEvent] = []

        def explode_database() -> None:
            calls.append("observation_persistence")
            raise RuntimeError("database-secret-canary")

        def capture_event(_workspace: WorkspaceRoot, event: RuntimeEvent) -> bool:
            events.append(event)
            return True

        monkeypatch.setattr(observation_module, "require_database", explode_database)
        monkeypatch.setattr(observation_module, "require_workspace", lambda: workspace)
        monkeypatch.setattr(observation_module, "try_append_runtime_event", capture_event)

        sentinel = {"structuredContent": {"ok": True}, "isError": False}
        assert _run(_Ctx("tools/call", {"name": "t", "arguments": {}}), sentinel) is sentinel

        assert calls == ["observation_persistence"], "the injected database failure never fired"
        failure = events[0]
        assert isinstance(failure, ObservationWriteFailedEvent)
        assert failure.correlation_id.startswith("fm_corr_")
        assert failure.tool_name == "t"
        assert failure.stage == "observation_persistence"
        assert failure.server_boot_id
        assert failure.process_id > 0
        assert "database-secret-canary" not in failure.model_dump_json()
        assert "database-secret-canary" not in caplog.text

    @pytest.mark.parametrize(
        ("failure_kind", "expected_stage"),
        [
            ("result_interpretation", "result_interpretation"),
            ("result_size", "result_size"),
            ("client_identity", "client_identity"),
        ],
    )
    def test_other_post_call_failures_also_preserve_the_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
        workspace: WorkspaceRoot,
        failure_kind: str,
        expected_stage: str,
    ) -> None:
        calls: list[str] = []
        events: list[RuntimeEvent] = []
        rows: list[object] = []

        def capture_event(_workspace: WorkspaceRoot, event: RuntimeEvent) -> bool:
            events.append(event)
            return True

        sentinel: object = {"structuredContent": {"ok": True}, "isError": False}
        if failure_kind == "client_identity":

            def explode_identity(_ctx: object) -> tuple[str | None, str | None]:
                calls.append("client_identity")
                raise RuntimeError("identity-secret-canary")

            monkeypatch.setattr(observation_module, "_client_identity", explode_identity)
        if failure_kind == "result_interpretation":

            def explode_interpretation(_method: str, _result: object) -> object:
                calls.append("result_interpretation")
                raise RuntimeError("interpretation-secret-canary")

            monkeypatch.setattr(observation_module, "_interpret_result", explode_interpretation)
        if failure_kind == "result_size":

            def explode_size(_result: object) -> int:
                calls.append("result_size")
                raise RuntimeError("size-secret-canary")

            monkeypatch.setattr(observation_module, "_measure_serialized_bytes", explode_size)
        monkeypatch.setattr(observation_module, "_record", rows.append)
        monkeypatch.setattr(observation_module, "require_workspace", lambda: workspace)
        monkeypatch.setattr(observation_module, "try_append_runtime_event", capture_event)

        result = _run(_Ctx("tools/call", {"name": "t", "arguments": {}}), sentinel)

        assert result is sentinel
        assert calls == [expected_stage], "the injected post-call failure never fired"
        assert len(rows) == 1
        failure = events[0]
        assert isinstance(failure, ObservationWriteFailedEvent)
        assert failure.stage == expected_stage
        assert "secret-canary" not in failure.model_dump_json()

    def test_telemetry_failure_never_replaces_the_original_raised_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original = ValueError("original protocol failure")
        middleware = CallObservationMiddleware()

        async def call_next(_ctx: object) -> object:
            raise original

        def explode_record(_write: object) -> None:
            raise RuntimeError("telemetry failed")

        def ignore_failure(_exc: BaseException, _call: object, _stage: object) -> None:
            pass

        monkeypatch.setattr(observation_module, "_record", explode_record)
        monkeypatch.setattr(observation_module, "_report_telemetry_failure", ignore_failure)

        async def main() -> None:
            await middleware(_Ctx("tools/call", {"name": "t", "arguments": {}}), call_next)

        with pytest.raises(ValueError, match="original protocol failure") as raised:
            anyio.run(main)
        assert raised.value is original


class TestCorrelationId:
    def test_the_id_is_visible_to_the_tool_boundary_during_a_call(
        self, recorded: list[dict[str, Any]]
    ) -> None:
        """The two layers share one id so a sanitised INTERNAL_ERROR envelope
        can be matched to its observation row.
        """
        seen: list[str] = []
        middleware = CallObservationMiddleware()

        async def call_next(_ctx: Any) -> Any:
            seen.append(current_correlation_id())
            return {"isError": False}

        async def main() -> None:
            await middleware(_Ctx("tools/call", {"name": "t", "arguments": {}}), call_next)

        anyio.run(main)

        assert seen == [recorded[0]["correlation_id"]]

    def test_outside_a_call_a_fresh_id_is_minted(self) -> None:
        """Calling ``tool.fn`` directly (unit tests) must not raise inside an
        error handler just because no middleware set the context.
        """
        assert current_correlation_id() != current_correlation_id()


class TestLifecycleMiddleware:
    def test_successful_initialize_records_only_safe_client_metadata(
        self,
        monkeypatch: pytest.MonkeyPatch,
        workspace: WorkspaceRoot,
    ) -> None:
        events: list[RuntimeEvent] = []

        def capture_event(_workspace: WorkspaceRoot, event: RuntimeEvent) -> bool:
            events.append(event)
            return True

        monkeypatch.setattr(observation_module, "require_workspace", lambda: workspace)
        monkeypatch.setattr(observation_module, "try_append_runtime_event", capture_event)
        ctx = _Ctx(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "Codex", "version": "1.2.3"},
                "capabilities": {"secret": "initialization-secret-canary"},
            },
        )
        middleware = LifecycleEventMiddleware()
        sentinel = {"protocolVersion": "2025-11-25"}

        async def call_next(_ctx: object) -> object:
            return sentinel

        async def main() -> object:
            return await middleware(ctx, call_next)

        assert anyio.run(main) is sentinel
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, ClientInitializedEvent)
        assert event.client_name == "Codex"
        assert event.client_version == "1.2.3"
        assert event.protocol_version == "2025-11-25"
        assert "initialization-secret-canary" not in event.model_dump_json()

    def test_failed_initialize_does_not_record_a_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[RuntimeEvent] = []

        def capture_event(_workspace: object, event: RuntimeEvent) -> None:
            events.append(event)

        monkeypatch.setattr(
            observation_module,
            "try_append_runtime_event",
            capture_event,
        )
        middleware = LifecycleEventMiddleware()

        async def call_next(_ctx: object) -> object:
            raise ValueError("invalid initialize")

        async def main() -> object:
            return await middleware(_Ctx("initialize", {}), call_next)

        with pytest.raises(ValueError, match="invalid initialize"):
            anyio.run(main)
        assert events == []
