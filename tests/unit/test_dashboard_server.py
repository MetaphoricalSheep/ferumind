"""Security and degradation tests for the loopback operator dashboard."""

from __future__ import annotations

import io
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest

from ferumind.core.diagnostics import DiagnosticsService
from ferumind.core.observations import record_mcp_call_observation
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.runtime_events import (
    ObservationWriteFailedEvent,
    append_runtime_event,
    internal_error_event,
)
from ferumind.core.types import JsonObject
from ferumind.dashboard.api import DashboardApi
from ferumind.dashboard.server import (
    DASHBOARD_HOST,
    DashboardHttpServer,
    DashboardRequestHandler,
)


@dataclass(frozen=True)
class _Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> JsonObject:
        return cast(JsonObject, json.loads(self.body))


class _HandlerHarness(DashboardRequestHandler):
    """Capture handler output without opening a socket in the test process."""

    response_status: int
    response_headers: dict[str, str]

    def send_response(self, code: int, message: str | None = None) -> None:
        del message
        self.response_status = code

    def send_header(self, keyword: str, value: str) -> None:
        self.response_headers[keyword.lower()] = value

    def end_headers(self) -> None:
        return


def _objects(value: object) -> list[JsonObject]:
    assert isinstance(value, list)
    items = cast("list[object]", value)
    assert all(isinstance(item, dict) for item in items)
    return cast("list[JsonObject]", items)


def _server(workspace: Path) -> DashboardHttpServer:
    server = object.__new__(DashboardHttpServer)
    root = WorkspaceRoot(workspace.resolve())
    server.dashboard_api = DashboardApi(DiagnosticsService(root))
    return server


def _request(
    server: DashboardHttpServer,
    path: str,
    *,
    method: str = "GET",
    host: str = "localhost:8765",
) -> _Response:
    handler = object.__new__(_HandlerHarness)
    handler.server = server
    handler.path = path
    handler.command = method
    handler.request_version = "HTTP/1.1"
    handler.response_status = 0
    handler.response_headers = {}
    handler.wfile = io.BytesIO()
    headers = HTTPMessage()
    headers.add_header("Host", host)
    handler.headers = headers
    if method == "GET":
        handler.do_GET()
    elif method == "HEAD":
        handler.do_HEAD()
    elif method == "POST":
        handler.do_POST()
    else:
        raise AssertionError(f"Unsupported harness method: {method}")
    return _Response(
        status=handler.response_status,
        headers=handler.response_headers,
        body=handler.wfile.getvalue(),
    )


def _assert_security_headers(response: _Response) -> None:
    policy = response.headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert "access-control-allow-origin" not in response.headers


def test_server_constructor_binds_only_ipv4_loopback(
    workspace: WorkspaceRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    addresses: list[tuple[str, int]] = []

    def capture_init(
        instance: ThreadingHTTPServer,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        bind_and_activate: bool = True,
    ) -> None:
        del instance, handler, bind_and_activate
        addresses.append(address)

    monkeypatch.setattr(ThreadingHTTPServer, "__init__", capture_init)
    api = DashboardApi(DiagnosticsService(workspace))
    server = DashboardHttpServer(9123, api)

    assert server.dashboard_api is api
    assert addresses == [("127.0.0.1", 9123)]
    assert DASHBOARD_HOST == "127.0.0.1"


def test_server_serves_only_explicit_packaged_assets(workspace: WorkspaceRoot) -> None:
    server = _server(workspace)
    index = _request(server, "/")
    stylesheet = _request(server, "/static/basecoat/tokens.css")
    script = _request(server, "/static/dashboard.js")
    missing = _request(server, "/static/basecoat/README.md")

    assert index.status == 200
    assert b"Ferumind" in index.body
    assert stylesheet.status == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert script.status == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert missing.status == 404
    _assert_security_headers(index)


def test_head_host_methods_and_traversal_are_restricted(workspace: WorkspaceRoot) -> None:
    server = _server(workspace)
    head = _request(server, "/", method="HEAD")
    rejected_host = _request(server, "/", host="operator.example")
    mutation = _request(server, "/api/v1/meta", method="POST")
    traversal = _request(server, "/static/%2e%2e/pyproject.toml")

    assert head.status == 200
    assert not head.body
    assert int(head.headers["content-length"]) > 0
    assert rejected_host.status == 400
    assert rejected_host.json()["error_code"] == "HOST_REJECTED"
    assert mutation.status == 405
    assert mutation.headers["allow"] == "GET, HEAD"
    assert traversal.status == 404
    assert b"pyproject" not in traversal.body
    _assert_security_headers(mutation)


def test_all_api_reports_render_without_a_running_mcp_process(workspace: WorkspaceRoot) -> None:
    routes = (
        "/api/v1/meta",
        "/api/v1/overview?window=1h",
        "/api/v1/calls?limit=5",
        "/api/v1/errors?window=7d",
        "/api/v1/performance?window=30d",
        "/api/v1/runtime?limit=5",
        "/api/v1/observations/fm_corr_missing",
    )
    server = _server(workspace)
    responses = tuple(_request(server, route) for route in routes)

    assert all(response.status == 200 for response in responses)
    lifecycle = responses[1].json()["lifecycle"]
    assert isinstance(lifecycle, dict)
    assert lifecycle["certainty"] in {"unknown", "inferred", "observed"}
    assert responses[-1].json()["found"] is False
    for response in responses:
        assert response.headers["cache-control"] == "no-store"
        _assert_security_headers(response)


def test_runtime_api_applies_window_and_limit(workspace: WorkspaceRoot) -> None:
    now = datetime.now(UTC)
    events = (
        ObservationWriteFailedEvent(
            timestamp=now - timedelta(days=2),
            correlation_id="fm_corr_runtime_old",
            tool_name="get_context",
            exception_type="sqlite3.OperationalError",
        ),
        ObservationWriteFailedEvent(
            timestamp=now - timedelta(minutes=2),
            correlation_id="fm_corr_runtime_recent_one",
            tool_name="get_context",
            exception_type="sqlite3.OperationalError",
        ),
        ObservationWriteFailedEvent(
            timestamp=now - timedelta(minutes=1),
            correlation_id="fm_corr_runtime_recent_two",
            tool_name="get_context",
            exception_type="sqlite3.OperationalError",
        ),
    )
    for event in events:
        append_runtime_event(workspace, event)

    server = _server(workspace)
    one_hour = _request(server, "/api/v1/runtime?window=1h&limit=1")
    thirty_days = _request(server, "/api/v1/runtime?window=30d&limit=10")

    assert one_hour.status == 200
    assert [event["correlation_id"] for event in _objects(one_hour.json()["events"])] == [
        "fm_corr_runtime_recent_two"
    ]
    assert thirty_days.status == 200
    assert [event["correlation_id"] for event in _objects(thirty_days.json()["events"])] == [
        "fm_corr_runtime_recent_two",
        "fm_corr_runtime_recent_one",
        "fm_corr_runtime_old",
    ]


@pytest.mark.parametrize(
    "query",
    [
        "project=demo",
        "tool=get_context",
        "client=Codex",
        "error_code=INTERNAL_ERROR",
        "status=all",
        "failed=false",
        "offset=0",
    ],
)
def test_runtime_api_rejects_explicit_unsupported_filters(
    workspace: WorkspaceRoot,
    query: str,
) -> None:
    response = _request(_server(workspace), f"/api/v1/runtime?{query}")

    assert response.status == 400
    assert response.json()["error_code"] == "INVALID_QUERY"


def test_missing_database_and_runtime_log_return_degraded_metadata(tmp_path: Path) -> None:
    missing_workspace = tmp_path / "missing-workspace"
    server = _server(missing_workspace)
    meta = _request(server, "/api/v1/meta")
    overview = _request(server, "/api/v1/overview")
    runtime = _request(server, "/api/v1/runtime")

    assert meta.status == 200
    diagnostic_meta = meta.json()["diagnostics"]
    assert isinstance(diagnostic_meta, dict)
    assert diagnostic_meta["workspace_available"] is False
    assert diagnostic_meta["database_available"] is False
    assert overview.status == 200
    assert overview.json()["degradations"]
    assert runtime.status == 200
    assert runtime.json()["log_available"] is False
    assert not missing_workspace.exists()


def test_query_validation_and_internal_errors_are_caller_safe(
    workspace: WorkspaceRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "SUPER_SECRET_EXCEPTION_MESSAGE"
    server = _server(workspace)
    invalid = _request(server, "/api/v1/calls?limit=501")
    duplicate = _request(server, "/api/v1/calls?window=1h&window=7d")

    def explode(_path: str, _query: str) -> None:
        raise RuntimeError(canary)

    monkeypatch.setattr(server.dashboard_api, "dispatch", explode)
    failed = _request(server, "/api/v1/meta")

    assert invalid.status == 400
    assert invalid.json()["error_code"] == "INVALID_QUERY"
    assert duplicate.status == 400
    assert failed.status == 500
    assert failed.json()["error_code"] == "INTERNAL_ERROR"
    assert canary.encode() not in failed.body
    assert b"Traceback" not in failed.body


def test_http_api_keeps_argument_and_exception_canaries_private(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    canary = "SIGNED_URL_ARGUMENT_AND_EXCEPTION_CANARY"
    correlation_id = "fm_corr_http_privacy"
    record_mcp_call_observation(
        conn,
        tool_name="read_document",
        correlation_id=correlation_id,
        ok=False,
        error_code="INTERNAL_ERROR",
        argument_keys=["project", "authorization"],
        context_metrics={"authorization": canary, "document_count": 1},
    )
    try:
        raise RuntimeError(canary)
    except RuntimeError as exc:
        append_runtime_event(workspace, internal_error_event(exc, correlation_id))

    server = _server(workspace)
    responses = (
        _request(server, "/api/v1/calls"),
        _request(server, "/api/v1/errors"),
        _request(server, "/api/v1/runtime"),
        _request(server, f"/api/v1/observations/{correlation_id}"),
    )
    rendered = b"\n".join(response.body for response in responses)

    assert all(response.status == 200 for response in responses)
    assert canary.encode() not in rendered
    assert b"[redacted]" in rendered
    assert b"authorization" in rendered  # Argument key names remain useful metadata.
