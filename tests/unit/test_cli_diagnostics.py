"""Operator CLI tests over the shared diagnostic service."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from ferumind.cli.main import app
from ferumind.core.observations import record_mcp_call_observation
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.runtime_events import ObservationWriteFailedEvent, append_runtime_event
from ferumind.core.types import JsonObject

runner = CliRunner()


def _json_output(output: str) -> JsonObject:
    return cast(JsonObject, json.loads(output))


def _object(value: object) -> JsonObject:
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def _objects(value: object) -> list[JsonObject]:
    assert isinstance(value, list)
    items = cast(list[object], value)
    assert all(isinstance(item, dict) for item in items)
    return cast(list[JsonObject], items)


def test_doctor_is_useful_with_an_empty_workspace(workspace: WorkspaceRoot) -> None:
    result = runner.invoke(app, ["doctor", "--workspace", str(workspace), "--json"])

    assert result.exit_code == 0, result.output
    report = _json_output(result.output)
    assert _object(report["meta"])["workspace_available"] is True
    assert _object(report["last_hour"])["calls"] == 0
    assert _object(report["last_24_hours"])["failures"] == 0
    assert "lifecycle" in report


def test_observations_list_errors_and_show_use_correlation_id(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    correlation_id = "lat_corr_historical_opaque"
    record_mcp_call_observation(
        conn,
        tool_name="read_document",
        correlation_id=correlation_id,
        project_key="demo",
        ok=False,
        error_code="DOCUMENT_NOT_FOUND",
        client_name="test-client",
        duration_ms=12.5,
        result_bytes=321,
        argument_keys=["project", "path"],
    )
    record_mcp_call_observation(
        conn,
        tool_name="read_document",
        correlation_id="fm_corr_other_failure",
        project_key="demo",
        ok=False,
        error_code="INTERNAL_ERROR",
        client_name="test-client",
    )

    common = [
        "observations",
        "--workspace",
        str(workspace),
        "--project",
        "demo",
        "--tool",
        "read_document",
        "--client",
        "test-client",
    ]
    listed = runner.invoke(app, [*common, "list", "--failed", "--json"])
    errors = runner.invoke(
        app,
        [*common, "errors", "--error-code", "DOCUMENT_NOT_FOUND", "--json"],
    )
    shown = runner.invoke(
        app,
        ["observations", "--workspace", str(workspace), "show", correlation_id, "--json"],
    )

    assert listed.exit_code == 0, listed.output
    observations = _objects(_json_output(listed.output)["observations"])
    listed_ids = {cast(str, item["correlation_id"]) for item in observations}
    assert correlation_id in listed_ids
    assert errors.exit_code == 0, errors.output
    groups = _objects(_json_output(errors.output)["error_code_groups"])
    assert groups[0]["error_code"] == "DOCUMENT_NOT_FOUND"
    assert shown.exit_code == 0, shown.output
    detail = _json_output(shown.output)
    assert detail["found"] is True
    assert _object(detail["observation"])["correlation_id"] == correlation_id


def test_observation_scope_is_per_invocation(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    for project_key in ("alpha", "beta"):
        record_mcp_call_observation(
            conn,
            tool_name="get_context",
            correlation_id=f"fm_corr_{project_key}",
            project_key=project_key,
            ok=True,
        )

    filtered = runner.invoke(
        app,
        [
            "observations",
            "--workspace",
            str(workspace),
            "--project",
            "alpha",
            "list",
            "--json",
        ],
    )
    unfiltered = runner.invoke(
        app,
        ["observations", "--workspace", str(workspace), "list", "--json"],
    )

    assert filtered.exit_code == 0, filtered.output
    assert _json_output(filtered.output)["total"] == 1
    assert unfiltered.exit_code == 0, unfiltered.output
    assert _json_output(unfiltered.output)["total"] == 2


def test_observation_time_filter_requires_a_timezone(workspace: WorkspaceRoot) -> None:
    result = runner.invoke(
        app,
        [
            "observations",
            "--workspace",
            str(workspace),
            "list",
            "--since",
            "2026-08-09T12:00:00",
        ],
    )

    assert result.exit_code == 2
    assert "must include a timezone" in result.output


def test_observation_help_places_shared_and_command_filters_consistently() -> None:
    shared = runner.invoke(app, ["observations", "--help"])
    listed = runner.invoke(app, ["observations", "list", "--help"])
    errors = runner.invoke(app, ["observations", "errors", "--help"])

    assert shared.exit_code == listed.exit_code == errors.exit_code == 0
    for option in ("--workspace", "--project", "--tool", "--client"):
        assert option in shared.output
    for option in ("--since", "--until", "--failed", "--limit", "--json"):
        assert option in listed.output
    assert "--error-code" not in listed.output
    assert "--error-code" in errors.output


def test_observations_show_missing_returns_nonzero(workspace: WorkspaceRoot) -> None:
    result = runner.invoke(
        app,
        ["observations", "--workspace", str(workspace), "show", "fm_corr_missing"],
    )

    assert result.exit_code == 1
    assert "No observation found" in result.output


def test_observations_show_runtime_only_failure_without_a_sqlite_row(
    workspace: WorkspaceRoot,
) -> None:
    correlation_id = "fm_corr_missing_observation_row"
    append_runtime_event(
        workspace,
        ObservationWriteFailedEvent(
            correlation_id=correlation_id,
            tool_name="get_context",
            exception_type="sqlite3.OperationalError",
        ),
    )

    result = runner.invoke(
        app,
        ["observations", "--workspace", str(workspace), "show", correlation_id],
    )

    assert result.exit_code == 0, result.output
    assert "SQLite observation unavailable" in result.output
    assert "observation_write_failed" in result.output
    assert "get_context / observation_persistence / sqlite3.OperationalError" in result.output


def test_human_observation_output_preserves_literal_markup_and_protocol(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    correlation_id = "fm_corr_literal_cli_metadata"
    markup_client = "[link=x]client[/link]"
    record_mcp_call_observation(
        conn,
        tool_name="read_document",
        correlation_id=correlation_id,
        project_key="demo",
        ok=False,
        error_code="DOCUMENT_NOT_FOUND",
        client_name=markup_client,
        client_version="1.0",
        protocol_version="2025-11-25",
    )

    listed = runner.invoke(
        app,
        ["observations", "--workspace", str(workspace), "list"],
        terminal_width=300,
    )
    shown = runner.invoke(
        app,
        ["observations", "--workspace", str(workspace), "show", correlation_id],
    )

    assert listed.exit_code == 0, listed.output
    # The narrow table may truncate the cell, but a raw Rich string would
    # consume the opening tag entirely instead of displaying it literally.
    assert "[link=" in listed.output
    assert "\x1b]8;" not in listed.output
    assert shown.exit_code == 0, shown.output
    assert f"Client: {markup_client} 1.0" in shown.output
    assert "Protocol: 2025-11-25" in shown.output


def test_observation_json_redacts_secret_metric_values(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    canary = "CANARY_AUTHORIZATION_SECRET"
    correlation_id = "fm_corr_private"
    record_mcp_call_observation(
        conn,
        tool_name="get_context",
        correlation_id=correlation_id,
        ok=True,
        context_metrics={"authorization": canary, "document_count": 3},
    )

    result = runner.invoke(
        app,
        [
            "observations",
            "--workspace",
            str(workspace),
            "show",
            correlation_id,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert canary not in result.output
    assert "[redacted]" in result.output


def test_dashboard_command_forwards_only_loopback_server_options(
    workspace: WorkspaceRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, int, bool]] = []

    def fake_serve(workspace_path: Path, *, port: int, open_browser: bool) -> None:
        calls.append((workspace_path, port, open_browser))

    # The command imports this function lazily so server startup stays out of other CLI calls.
    from ferumind.dashboard import server

    monkeypatch.setattr(server, "serve_dashboard", fake_serve)
    result = runner.invoke(
        app,
        ["dashboard", "--workspace", str(workspace), "--port", "9876", "--open"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(Path(workspace), 9876, True)]
