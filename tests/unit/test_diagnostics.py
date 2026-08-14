"""Focused tests for the shared read-only diagnostic query layer."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ferumind.core.diagnostic_models import DiagnosticQuery
from ferumind.core.diagnostics import DiagnosticsService
from ferumind.core.observations import record_mcp_call_observation
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.runtime_events import (
    ClientInitializedEvent,
    InternalErrorEvent,
    ObservationWriteFailedEvent,
    ProcessStartedEvent,
    ProcessStoppingEvent,
    append_runtime_event,
    runtime_log_path,
)
from ferumind.db.database import Database


@dataclass(frozen=True)
class _Seed:
    correlation_id: str
    created_at: datetime
    tool_name: str = "get_context"
    project_key: str | None = "demo"
    ok: bool | None = True
    error_code: str | None = None
    client_name: str | None = "Codex"
    duration_ms: float | None = None
    result_bytes: int | None = None


def _seed(conn: sqlite3.Connection, seed: _Seed) -> str:
    observation_id = record_mcp_call_observation(
        conn,
        correlation_id=seed.correlation_id,
        tool_name=seed.tool_name,
        project_key=seed.project_key,
        ok=seed.ok,
        error_code=seed.error_code,
        client_name=seed.client_name,
        duration_ms=seed.duration_ms,
        result_bytes=seed.result_bytes,
        argument_keys=["project"],
        context_metrics={"documents_count": 2},
    )
    conn.execute(
        "UPDATE mcp_call_observations SET created_at = ? WHERE id = ?",
        (seed.created_at.isoformat(), observation_id),
    )
    conn.commit()
    return observation_id


def test_empty_and_missing_diagnostics_degrade_without_raising(
    workspace: WorkspaceRoot, database: Database, tmp_path: Path
) -> None:
    service = DiagnosticsService(workspace)
    meta = service.meta()
    calls = service.calls(DiagnosticQuery(window="1h"))
    performance = service.performance(DiagnosticQuery(window="1h"))

    assert meta.database_available is True
    assert meta.observation_count == 0
    assert meta.database_size_bytes is not None
    assert meta.runtime_log_available is False
    assert {item.code for item in meta.degradations} == {"runtime_log_missing"}
    assert calls.total == 0
    assert calls.observations == ()
    assert performance.latency.sample_count == 0
    assert performance.latency.p50_ms is None

    missing = DiagnosticsService(WorkspaceRoot(tmp_path / "missing-workspace"))
    missing_meta = missing.meta()
    assert missing_meta.workspace_available is False
    assert missing_meta.database_available is False
    assert {item.code for item in missing_meta.degradations} == {
        "workspace_missing",
        "database_missing",
        "runtime_log_missing",
    }


def test_query_filters_windows_groups_and_buckets(
    conn: sqlite3.Connection, workspace: WorkspaceRoot
) -> None:
    end = datetime.now(UTC) + timedelta(seconds=1)
    seeds = (
        _Seed("fm_corr_recent", end - timedelta(minutes=30), duration_ms=10),
        _Seed(
            "fm_corr_failed",
            end - timedelta(hours=2),
            tool_name="apply_patch",
            project_key="other",
            ok=False,
            error_code="PATCH_CONFLICT",
            client_name="Claude",
            duration_ms=20,
        ),
        _Seed("fm_corr_two_days", end - timedelta(days=2), duration_ms=30),
        _Seed("fm_corr_ten_days", end - timedelta(days=10), duration_ms=40),
        _Seed("fm_corr_old", end - timedelta(days=40), duration_ms=50),
    )
    for seed in seeds:
        _seed(conn, seed)

    service = DiagnosticsService(workspace)
    assert service.calls(DiagnosticQuery(window="1h")).total == 1
    assert service.calls(DiagnosticQuery(window="24h")).total == 2
    assert service.calls(DiagnosticQuery(window="7d")).total == 3
    assert service.calls(DiagnosticQuery(window="30d")).total == 4

    range_start = end - timedelta(days=1)
    failed = service.calls(
        DiagnosticQuery(
            start=range_start,
            end=end,
            failed=True,
            error_code="PATCH_CONFLICT",
        )
    )
    assert failed.total == 1
    assert failed.observations[0].correlation_id == "fm_corr_failed"

    filtered = service.calls(
        DiagnosticQuery(
            start=range_start,
            end=end,
            project="other",
            tool="apply_patch",
            client="Claude",
        )
    )
    assert filtered.total == 1
    assert filtered.grouped_by_tool[0].dimension == "apply_patch"
    assert filtered.grouped_by_project[0].dimension == "other"
    assert filtered.grouped_by_client[0].dimension == "Claude"

    overview = service.overview(DiagnosticQuery(start=range_start, end=end))
    assert overview.summary.calls == 2
    assert overview.summary.failures == 1
    assert overview.summary.failure_rate == 0.5
    assert sum(bucket.calls for bucket in overview.activity) == 2
    assert sum(bucket.failures for bucket in overview.activity) == 1


def test_percentiles_outliers_and_sample_counts(
    conn: sqlite3.Connection, workspace: WorkspaceRoot
) -> None:
    end = datetime.now(UTC) + timedelta(seconds=1)
    for index, duration in enumerate((10.0, 20.0, 30.0, 40.0), start=1):
        _seed(
            conn,
            _Seed(
                f"fm_corr_{index}",
                end - timedelta(minutes=index),
                tool_name="read_document",
                duration_ms=duration,
                result_bytes=index * 100,
            ),
        )

    report = DiagnosticsService(workspace).performance(
        DiagnosticQuery(start=end - timedelta(hours=1), end=end, limit=3)
    )
    assert report.call_count == 4
    assert report.latency.sample_count == 4
    assert report.latency.p50_ms == 25.0
    assert report.latency.p95_ms == pytest.approx(38.5)
    assert report.latency.max_ms == 40.0
    assert report.response_sizes.p50_bytes == 250.0
    assert report.response_sizes.max_bytes == 400
    assert [item.duration_ms for item in report.slowest_calls] == [40.0, 30.0, 20.0]
    assert [item.result_bytes for item in report.largest_responses] == [400, 300, 200]
    assert report.calls_by_tool[0].latency.sample_count == 4


def test_failure_and_runtime_fingerprint_grouping(
    conn: sqlite3.Connection, workspace: WorkspaceRoot
) -> None:
    end = datetime.now(UTC) + timedelta(seconds=1)
    first = _Seed(
        "fm_corr_internal_one",
        end - timedelta(minutes=3),
        tool_name="get_context",
        ok=False,
        error_code="INTERNAL_ERROR",
        duration_ms=5,
    )
    second = _Seed(
        "fm_corr_internal_two",
        end - timedelta(minutes=2),
        tool_name="read_document",
        project_key="other",
        ok=False,
        error_code="INTERNAL_ERROR",
        duration_ms=6,
    )
    conflict = _Seed(
        "fm_corr_conflict",
        end - timedelta(minutes=1),
        tool_name="apply_patch",
        ok=False,
        error_code="PATCH_CONFLICT",
        duration_ms=7,
    )
    for seed in (first, second, conflict):
        _seed(conn, seed)

    for seed in (first, second):
        append_runtime_event(
            workspace,
            InternalErrorEvent(
                timestamp=seed.created_at,
                correlation_id=seed.correlation_id,
                exception_type="builtins.RuntimeError",
                stack_fingerprint="fm_stack_same",
                frames=(),
            ),
        )
    append_runtime_event(
        workspace,
        ObservationWriteFailedEvent(
            timestamp=end - timedelta(seconds=30),
            correlation_id="fm_corr_missing_row",
            tool_name="search_project",
            exception_type="sqlite3.OperationalError",
        ),
    )

    report = DiagnosticsService(workspace).errors(
        DiagnosticQuery(start=end - timedelta(hours=1), end=end, limit=20)
    )
    assert report.failure_count == 3
    assert [(group.error_code, group.count) for group in report.error_code_groups] == [
        ("INTERNAL_ERROR", 2),
        ("PATCH_CONFLICT", 1),
    ]
    fingerprint = report.internal_error_groups[0]
    assert fingerprint.stack_fingerprint == "fm_stack_same"
    assert fingerprint.count == 2
    assert fingerprint.affected_tools == ("get_context", "read_document")
    assert fingerprint.affected_projects == ("demo", "other")
    assert len(report.recent_internal_errors) == 2
    assert len(report.observation_write_failures) == 1

    scoped = DiagnosticsService(workspace).errors(
        DiagnosticQuery(
            start=end - timedelta(hours=1),
            end=end,
            project="demo",
            limit=20,
        )
    )
    assert len(scoped.internal_error_groups) == 1
    assert scoped.internal_error_groups[0].count == 1
    assert scoped.internal_error_groups[0].affected_tools == ("get_context",)
    assert scoped.observation_write_failures == ()

    telemetry = DiagnosticsService(workspace).errors(
        DiagnosticQuery(
            start=end - timedelta(hours=1),
            end=end,
            tool="search_project",
            limit=20,
        )
    )
    assert telemetry.internal_error_groups == ()
    assert len(telemetry.observation_write_failures) == 1

    expected_only = DiagnosticsService(workspace).errors(
        DiagnosticQuery(
            start=end - timedelta(hours=1),
            end=end,
            error_code="PATCH_CONFLICT",
            limit=20,
        )
    )
    assert expected_only.internal_error_groups == ()
    assert expected_only.observation_write_failures == ()


def test_exact_detail_supports_historical_id_and_safe_runtime_event(
    conn: sqlite3.Connection, workspace: WorkspaceRoot
) -> None:
    timestamp = datetime.now(UTC)
    observation_id = _seed(
        conn,
        _Seed(
            "lat_corr_historical",
            timestamp,
            ok=False,
            error_code="INTERNAL_ERROR",
        ),
    )
    append_runtime_event(
        workspace,
        InternalErrorEvent(
            timestamp=timestamp,
            correlation_id="lat_corr_historical",
            exception_type="builtins.ValueError",
            stack_fingerprint="fm_stack_historical",
            frames=(),
        ),
    )

    detail = DiagnosticsService(workspace).observation("lat_corr_historical")
    assert detail.found is True
    assert detail.observation is not None
    assert detail.observation.id == observation_id
    assert detail.observation.argument_keys == ("project",)
    assert detail.observation.context_metrics == {"documents_count": 2}
    assert len(detail.runtime_events) == 1
    assert detail.model_dump(mode="json")["correlation_id"] == "lat_corr_historical"


def test_lifecycle_and_malformed_runtime_lines_degrade_precisely(
    workspace: WorkspaceRoot, database: Database
) -> None:
    started = datetime.now(UTC) - timedelta(seconds=3)
    append_runtime_event(
        workspace,
        ProcessStartedEvent(timestamp=started, package_version="0.1.0"),
    )
    append_runtime_event(
        workspace,
        ClientInitializedEvent(timestamp=started + timedelta(seconds=1), client_name="Codex"),
    )
    append_runtime_event(
        workspace,
        ProcessStoppingEvent(timestamp=started + timedelta(seconds=2)),
    )
    with runtime_log_path(workspace).open("ab") as handle:
        handle.write(b'{"event":"internal_error"')

    report = DiagnosticsService(workspace).runtime()
    assert report.log_available is True
    assert report.lifecycle.status == "stopped_cleanly"
    assert report.malformed_lines == 1
    assert {item.code for item in report.degradations} == {"runtime_log_malformed"}


def test_overview_and_doctor_use_lifecycle_and_observation_history_before_window(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    started = datetime.now(UTC) - timedelta(days=2)
    _seed(
        conn,
        _Seed(
            correlation_id="fm_corr_before_window",
            created_at=started - timedelta(minutes=1),
        ),
    )
    append_runtime_event(
        workspace,
        ProcessStartedEvent(timestamp=started, package_version="0.1.0"),
    )

    overview = DiagnosticsService(workspace).overview(DiagnosticQuery(window="24h"))
    doctor = DiagnosticsService(workspace).doctor()

    assert overview.summary.calls == 0
    assert overview.latest_observation is not None
    assert overview.latest_observation.correlation_id == "fm_corr_before_window"
    assert overview.latest_runtime_event is None
    assert overview.lifecycle.status == "started"
    assert overview.lifecycle.observed_at == started
    assert doctor.lifecycle.status == "started"
    assert doctor.lifecycle.observed_at == started


def test_runtime_lifecycle_is_not_displaced_by_visible_event_limit(
    workspace: WorkspaceRoot,
    database: Database,
) -> None:
    started = datetime.now(UTC) - timedelta(minutes=5)
    append_runtime_event(
        workspace,
        ProcessStartedEvent(timestamp=started, package_version="0.1.0"),
    )
    for index in range(3):
        append_runtime_event(
            workspace,
            ObservationWriteFailedEvent(
                timestamp=started + timedelta(seconds=index + 1),
                correlation_id=f"fm_corr_displacing_{index}",
                tool_name="get_context",
                exception_type="sqlite3.OperationalError",
            ),
        )

    report = DiagnosticsService(workspace).runtime(DiagnosticQuery(window="1h", limit=2))

    assert len(report.events) == 2
    assert all(isinstance(event, ObservationWriteFailedEvent) for event in report.events)
    assert report.lifecycle.status == "started"
    assert report.lifecycle.observed_at == started


def test_diagnostics_never_initialize_or_migrate_database(tmp_path: Path) -> None:
    workspace = WorkspaceRoot(tmp_path / "workspace")
    db_path = workspace / ".ferumind" / "ferumind.sqlite"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE mcp_call_observations (created_at TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO mcp_call_observations (created_at) VALUES ('2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    service = DiagnosticsService(workspace)
    meta = service.meta()
    assert meta.database_available is True
    assert meta.observation_count == 1
    calls = service.calls(
        DiagnosticQuery(
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2027, 1, 1, tzinfo=UTC),
        )
    )
    assert {item.code for item in calls.degradations} == {"database_unavailable"}

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        indexes = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
        columns = conn.execute("PRAGMA table_info(mcp_call_observations)").fetchall()
    finally:
        conn.close()
    assert indexes == []
    assert [column[1] for column in columns] == ["created_at"]


def test_corrupt_database_and_query_validation_are_safe(tmp_path: Path) -> None:
    workspace = WorkspaceRoot(tmp_path / "workspace")
    db_path = workspace / ".ferumind" / "ferumind.sqlite"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"not a sqlite database")

    meta = DiagnosticsService(workspace).meta()
    assert meta.database_available is False
    assert {item.code for item in meta.degradations} >= {"database_unavailable"}

    with pytest.raises(ValidationError, match="timezone"):
        DiagnosticQuery(start=datetime(2026, 1, 1))
    with pytest.raises(ValidationError, match="earlier"):
        DiagnosticQuery(
            start=datetime(2026, 1, 2, tzinfo=UTC),
            end=datetime(2026, 1, 1, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        DiagnosticQuery(limit=501)
