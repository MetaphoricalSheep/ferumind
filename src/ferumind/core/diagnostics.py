"""Read-only, metadata-only operator diagnostics.

All observation SQL used by the CLI and local dashboard lives here.  The
service opens SQLite through its URI read-only connection, never initializes
or migrates a database, and converts missing/corrupt inputs into typed
degradations.  Runtime data is read exclusively through ``runtime_events`` so
its privacy and record-size boundary stays authoritative.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import TypeAdapter

from ferumind.core.diagnostic_models import (
    ActivityBucket,
    CallsReport,
    CountSummary,
    DegradationCode,
    DiagnosticComponent,
    DiagnosticDegradation,
    DiagnosticObservation,
    DiagnosticQuery,
    DiagnosticRange,
    DiagnosticsMeta,
    DoctorReport,
    ErrorCodeGroup,
    ErrorsReport,
    GroupedCallMetrics,
    LatencyStats,
    LifecycleCertainty,
    LifecycleState,
    LifecycleStatus,
    ObservationDetailReport,
    OverviewReport,
    PerformanceReport,
    ResponseSizeStats,
    RuntimeErrorGroup,
    RuntimeReport,
)
from ferumind.core.observations import (
    McpCallObservationRecord,
    get_observation_by_correlation_id,
)
from ferumind.core.paths import PathSafetyError, WorkspaceRoot, contained_path
from ferumind.core.runtime_events import (
    MAX_RUNTIME_EVENT_LIMIT,
    InternalErrorEvent,
    ObservationWriteFailedEvent,
    ProcessStartedEvent,
    ProcessStoppingEvent,
    RuntimeEvent,
    RuntimeEventBatch,
    RuntimeEventFilter,
    RuntimeEventQuery,
    read_runtime_events,
)
from ferumind.core.types import DbConnection, DbRow, JsonObject
from ferumind.db.database import Database

DEFAULT_DIAGNOSTIC_LIMIT = 50
MAX_DIAGNOSTIC_LIMIT = 500
DEFAULT_RUNTIME_LIMIT = 200

_WINDOWS = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_STRING_TUPLE_ADAPTER: TypeAdapter[tuple[str, ...]] = TypeAdapter(tuple[str, ...])

type _SqlValue = str | int | float | None
type _MetricColumn = str


def _now() -> datetime:
    return datetime.now(UTC)


def _failure_rate(calls: int, failures: int) -> float:
    return failures / calls if calls else 0.0


def _resolved_range(query: DiagnosticQuery, now: datetime) -> DiagnosticRange:
    end = (query.end or now).astimezone(UTC)
    start = (query.start or (end - _WINDOWS[query.window])).astimezone(UTC)
    return DiagnosticRange(start=start, end=end)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _degradation(
    component: DiagnosticComponent,
    code: DegradationCode,
    message: str,
    affected: int | None = None,
) -> DiagnosticDegradation:
    return DiagnosticDegradation(
        component=component,
        code=code,
        message=message,
        affected_records=affected,
    )


def _merge_degradations(
    *groups: Sequence[DiagnosticDegradation],
) -> tuple[DiagnosticDegradation, ...]:
    unique: dict[tuple[str, str], DiagnosticDegradation] = {}
    for group in groups:
        for item in group:
            unique[(item.component, item.code)] = item
    return tuple(unique.values())


@dataclass(frozen=True)
class _SqlFilter:
    clause: str
    params: tuple[_SqlValue, ...]

    def extended(self, clause: str, *params: _SqlValue) -> _SqlFilter:
        return _SqlFilter(f"{self.clause} AND {clause}", (*self.params, *params))


def _effective_status(query: DiagnosticQuery) -> str:
    if query.failed is True:
        return "failed"
    if query.failed is False:
        return "success"
    return query.status


def _observation_filter(query: DiagnosticQuery, value_range: DiagnosticRange) -> _SqlFilter:
    clauses = ["created_at >= ?", "created_at < ?"]
    params: list[_SqlValue] = [_iso(value_range.start), _iso(value_range.end)]
    optional = (
        ("project_key = ?", query.project),
        ("tool_name = ?", query.tool),
        ("client_name = ?", query.client),
        ("error_code = ?", query.error_code),
    )
    for clause, value in optional:
        if value is not None:
            clauses.append(clause)
            params.append(value)
    status = _effective_status(query)
    status_clause = {"success": "ok = 1", "failed": "ok = 0", "unknown": "ok IS NULL"}
    if status in status_clause:
        clauses.append(status_clause[status])
    return _SqlFilter(" AND ".join(clauses), tuple(params))


def _decode_metadata(
    record: McpCallObservationRecord,
) -> tuple[JsonObject, tuple[str, ...], tuple[str, ...], bool]:
    degraded = False
    try:
        context = _JSON_OBJECT_ADAPTER.validate_json(record.context_metrics_json)
    except ValueError:
        context = {}
        degraded = True
    try:
        keys = _STRING_TUPLE_ADAPTER.validate_json(record.argument_keys_json)
    except ValueError:
        keys = ()
        degraded = True
    try:
        notes = _STRING_TUPLE_ADAPTER.validate_json(record.redaction_notes_json)
    except ValueError:
        notes = ()
        degraded = True
    return context, keys, notes, degraded


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.utcoffset() is None else parsed


def _diagnostic_observation(record: McpCallObservationRecord) -> DiagnosticObservation:
    context, keys, notes, degraded = _decode_metadata(record)
    return DiagnosticObservation(
        id=record.id,
        correlation_id=record.correlation_id,
        tool_name=record.tool_name,
        project_key=record.project_key,
        created_at=_timestamp(record.created_at),
        ok=record.ok,
        error_code=record.error_code,
        transport=record.transport,
        server_boot_id=record.server_boot_id,
        process_id=record.process_id,
        client_name=record.client_name,
        client_version=record.client_version,
        protocol_version=record.protocol_version,
        duration_ms=record.duration_ms,
        result_bytes=record.result_bytes,
        context_metrics=context,
        argument_keys=keys,
        redaction_notes=notes,
        metadata_degraded=degraded,
    )


def _observation_record(row: DbRow) -> McpCallObservationRecord:
    ok_value = row["ok"]
    return McpCallObservationRecord(
        id=row["id"],
        correlation_id=row["correlation_id"],
        tool_name=row["tool_name"],
        project_key=row["project_key"],
        created_at=row["created_at"],
        ok=None if ok_value is None else bool(ok_value),
        error_code=row["error_code"],
        transport=row["transport"],
        server_boot_id=row["server_boot_id"],
        process_id=row["process_id"],
        client_name=row["client_name"],
        client_version=row["client_version"],
        protocol_version=row["protocol_version"],
        duration_ms=row["duration_ms"],
        result_bytes=row["result_bytes"],
        context_metrics_json=row["context_metrics_json"],
        argument_keys_json=row["argument_keys_json"],
        redaction_notes_json=row["redaction_notes_json"],
    )


def _observations(rows: Sequence[DbRow]) -> tuple[DiagnosticObservation, ...]:
    return tuple(_diagnostic_observation(_observation_record(row)) for row in rows)


def _select_observations(
    conn: DbConnection,
    sql_filter: _SqlFilter,
    limit: int,
    offset: int,
    order: str,
) -> tuple[DiagnosticObservation, ...]:
    order_sql = {
        "recent": "created_at DESC, id DESC",
        "slowest": "duration_ms DESC, created_at DESC, id DESC",
        "largest": "result_bytes DESC, created_at DESC, id DESC",
    }[order]
    rows = conn.execute(
        f"""SELECT * FROM mcp_call_observations
            WHERE {sql_filter.clause}
            ORDER BY {order_sql} LIMIT ? OFFSET ?""",  # noqa: S608 - clauses are internal whitelists.
        (*sql_filter.params, limit, offset),
    ).fetchall()
    return _observations(rows)


def _count(conn: DbConnection, sql_filter: _SqlFilter) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM mcp_call_observations WHERE {sql_filter.clause}",  # noqa: S608 - internal clauses only.
        sql_filter.params,
    ).fetchone()
    return int(row["count"])


def _count_summary(conn: DbConnection, sql_filter: _SqlFilter) -> CountSummary:
    row = conn.execute(
        f"""SELECT COUNT(*) AS calls,
                   COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failures,
                   COALESCE(SUM(CASE WHEN error_code = 'INTERNAL_ERROR' THEN 1 ELSE 0 END), 0)
                       AS internal_errors
            FROM mcp_call_observations WHERE {sql_filter.clause}""",  # noqa: S608
        sql_filter.params,
    ).fetchone()
    calls = int(row["calls"])
    failures = int(row["failures"])
    return CountSummary(
        calls=calls,
        failures=failures,
        failure_rate=_failure_rate(calls, failures),
        internal_errors=int(row["internal_errors"]),
    )


def _percentile(
    conn: DbConnection,
    sql_filter: _SqlFilter,
    column: _MetricColumn,
    sample_count: int,
    percentile: float,
) -> float | None:
    if sample_count == 0:
        return None
    position = (sample_count - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    rows = conn.execute(
        f"""SELECT {column} AS value FROM mcp_call_observations
            WHERE {sql_filter.clause} AND {column} IS NOT NULL
            ORDER BY {column} LIMIT ? OFFSET ?""",  # noqa: S608 - column is internal.
        (*sql_filter.params, upper - lower + 1, lower),
    ).fetchall()
    low_value = float(rows[0]["value"])
    high_value = float(rows[-1]["value"])
    return low_value + (high_value - low_value) * (position - lower)


def _metric_values(
    conn: DbConnection, sql_filter: _SqlFilter, column: _MetricColumn
) -> tuple[int, float | None, float | None, float | None]:
    row = conn.execute(
        f"""SELECT COUNT({column}) AS sample_count, MAX({column}) AS maximum
            FROM mcp_call_observations WHERE {sql_filter.clause}""",  # noqa: S608
        sql_filter.params,
    ).fetchone()
    count = int(row["sample_count"])
    maximum = None if row["maximum"] is None else float(row["maximum"])
    return (
        count,
        _percentile(conn, sql_filter, column, count, 0.50),
        _percentile(conn, sql_filter, column, count, 0.95),
        maximum,
    )


def _latency_stats(conn: DbConnection, sql_filter: _SqlFilter) -> LatencyStats:
    count, p50, p95, maximum = _metric_values(conn, sql_filter, "duration_ms")
    return LatencyStats(sample_count=count, p50_ms=p50, p95_ms=p95, max_ms=maximum)


def _response_size_stats(conn: DbConnection, sql_filter: _SqlFilter) -> ResponseSizeStats:
    count, p50, p95, maximum = _metric_values(conn, sql_filter, "result_bytes")
    return ResponseSizeStats(
        sample_count=count,
        p50_bytes=p50,
        p95_bytes=p95,
        max_bytes=None if maximum is None else int(maximum),
    )


def _bucket_plan(value_range: DiagnosticRange) -> tuple[int, int]:
    duration = max(1, int((value_range.end - value_range.start).total_seconds()))
    if duration <= 2 * 60 * 60:
        size = 5 * 60
    elif duration <= 2 * 24 * 60 * 60:
        size = 60 * 60
    elif duration <= 14 * 24 * 60 * 60:
        size = 6 * 60 * 60
    else:
        size = 24 * 60 * 60
    count = math.ceil(duration / size)
    if count > 120:
        size = math.ceil(duration / 120)
        count = math.ceil(duration / size)
    return size, count


def _activity_buckets(
    conn: DbConnection, sql_filter: _SqlFilter, value_range: DiagnosticRange
) -> tuple[ActivityBucket, ...]:
    bucket_seconds, bucket_count = _bucket_plan(value_range)
    rows = conn.execute(
        f"""SELECT CAST((unixepoch(created_at) - unixepoch(?)) / ? AS INTEGER)
                       AS bucket_index,
                   COUNT(*) AS calls,
                   COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failures
            FROM mcp_call_observations
            WHERE {sql_filter.clause}
            GROUP BY bucket_index""",  # noqa: S608
        (_iso(value_range.start), bucket_seconds, *sql_filter.params),
    ).fetchall()
    counts = {
        int(row["bucket_index"]): (int(row["calls"]), int(row["failures"]))
        for row in rows
        if row["bucket_index"] is not None
    }
    buckets: list[ActivityBucket] = []
    for index in range(bucket_count):
        start = value_range.start + timedelta(seconds=index * bucket_seconds)
        end = min(start + timedelta(seconds=bucket_seconds), value_range.end)
        calls, failures = counts.get(index, (0, 0))
        buckets.append(ActivityBucket(start=start, end=end, calls=calls, failures=failures))
    return tuple(buckets)


def _grouped_call_metrics(
    conn: DbConnection, sql_filter: _SqlFilter, dimension: str
) -> tuple[GroupedCallMetrics, ...]:
    column = {"tool": "tool_name", "project": "project_key", "client": "client_name"}[dimension]
    rows = conn.execute(
        f"""SELECT {column} AS dimension, COUNT(*) AS calls,
                   COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failures
            FROM mcp_call_observations WHERE {sql_filter.clause}
            GROUP BY {column} ORDER BY calls DESC, {column} LIMIT 100""",  # noqa: S608
        sql_filter.params,
    ).fetchall()
    groups: list[GroupedCallMetrics] = []
    for row in rows:
        value = row["dimension"]
        group_filter = sql_filter.extended(
            f"{column} IS NULL" if value is None else f"{column} = ?",
            *(() if value is None else (str(value),)),
        )
        calls = int(row["calls"])
        failures = int(row["failures"])
        groups.append(
            GroupedCallMetrics(
                dimension=value,
                calls=calls,
                failures=failures,
                failure_rate=_failure_rate(calls, failures),
                latency=_latency_stats(conn, group_filter),
            )
        )
    return tuple(groups)


def _split_grouped_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        return ()
    return tuple(sorted(set(value.split(","))))


def _error_code_groups(conn: DbConnection, sql_filter: _SqlFilter) -> tuple[ErrorCodeGroup, ...]:
    failed_filter = sql_filter.extended("ok = 0")
    rows = conn.execute(
        f"""SELECT error_code, COUNT(*) AS count, MIN(created_at) AS first_seen,
                   MAX(created_at) AS last_seen,
                   GROUP_CONCAT(DISTINCT tool_name) AS tools,
                   GROUP_CONCAT(DISTINCT project_key) AS projects
            FROM mcp_call_observations WHERE {failed_filter.clause}
            GROUP BY error_code ORDER BY count DESC, error_code LIMIT 100""",  # noqa: S608
        failed_filter.params,
    ).fetchall()
    return tuple(
        ErrorCodeGroup(
            error_code=row["error_code"],
            count=row["count"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            affected_tools=_split_grouped_values(row["tools"]),
            affected_projects=_split_grouped_values(row["projects"]),
        )
        for row in rows
    )


@dataclass(frozen=True)
class _MetaData:
    observation_count: int
    latest_observation_at: datetime | None


@dataclass(frozen=True)
class _OverviewData:
    summary: CountSummary
    latency: LatencyStats
    response_sizes: ResponseSizeStats
    activity: tuple[ActivityBucket, ...]
    latest_observation: DiagnosticObservation | None
    latest_failure: DiagnosticObservation | None
    latest_client: str | None


@dataclass(frozen=True)
class _CallsData:
    total: int
    observations: tuple[DiagnosticObservation, ...]
    by_tool: tuple[GroupedCallMetrics, ...]
    by_project: tuple[GroupedCallMetrics, ...]
    by_client: tuple[GroupedCallMetrics, ...]


@dataclass(frozen=True)
class _ErrorsData:
    failure_count: int
    groups: tuple[ErrorCodeGroup, ...]
    recent: tuple[DiagnosticObservation, ...]
    recent_internal: tuple[DiagnosticObservation, ...]
    internal_group_observations: tuple[DiagnosticObservation, ...]


@dataclass(frozen=True)
class _PerformanceData:
    call_count: int
    latency: LatencyStats
    response_sizes: ResponseSizeStats
    by_tool: tuple[GroupedCallMetrics, ...]
    slowest: tuple[DiagnosticObservation, ...]
    largest: tuple[DiagnosticObservation, ...]


def _load_meta(conn: DbConnection) -> _MetaData:
    row = conn.execute(
        """SELECT COUNT(*) AS count, MAX(created_at) AS latest
           FROM mcp_call_observations"""
    ).fetchone()
    return _MetaData(
        observation_count=int(row["count"]),
        latest_observation_at=row["latest"],
    )


def _latest_client(conn: DbConnection, sql_filter: _SqlFilter) -> str | None:
    row = conn.execute(
        f"""SELECT client_name FROM mcp_call_observations
            WHERE {sql_filter.clause} AND client_name IS NOT NULL
            ORDER BY created_at DESC, id DESC LIMIT 1""",  # noqa: S608
        sql_filter.params,
    ).fetchone()
    return None if row is None else str(row["client_name"])


def _load_overview(
    conn: DbConnection, query: DiagnosticQuery, value_range: DiagnosticRange
) -> _OverviewData:
    sql_filter = _observation_filter(query, value_range)
    failures = _select_observations(conn, sql_filter.extended("ok = 0"), 1, 0, "recent")
    return _OverviewData(
        summary=_count_summary(conn, sql_filter),
        latency=_latency_stats(conn, sql_filter),
        response_sizes=_response_size_stats(conn, sql_filter),
        activity=_activity_buckets(conn, sql_filter, value_range),
        latest_observation=_load_latest_observation(conn),
        latest_failure=failures[0] if failures else None,
        latest_client=_latest_client(conn, sql_filter),
    )


def _load_calls(
    conn: DbConnection, query: DiagnosticQuery, value_range: DiagnosticRange
) -> _CallsData:
    sql_filter = _observation_filter(query, value_range)
    return _CallsData(
        total=_count(conn, sql_filter),
        observations=_select_observations(conn, sql_filter, query.limit, query.offset, "recent"),
        by_tool=_grouped_call_metrics(conn, sql_filter, "tool"),
        by_project=_grouped_call_metrics(conn, sql_filter, "project"),
        by_client=_grouped_call_metrics(conn, sql_filter, "client"),
    )


def _load_errors(
    conn: DbConnection, query: DiagnosticQuery, value_range: DiagnosticRange
) -> _ErrorsData:
    base_filter = _observation_filter(query, value_range)
    sql_filter = base_filter.extended("ok = 0")
    internal_filter = sql_filter.extended("error_code = ?", "INTERNAL_ERROR")
    return _ErrorsData(
        failure_count=_count(conn, sql_filter),
        groups=_error_code_groups(conn, base_filter),
        recent=_select_observations(conn, sql_filter, query.limit, query.offset, "recent"),
        recent_internal=_select_observations(
            conn,
            internal_filter,
            query.limit,
            query.offset,
            "recent",
        ),
        internal_group_observations=_select_observations(
            conn,
            internal_filter,
            MAX_RUNTIME_EVENT_LIMIT,
            0,
            "recent",
        ),
    )


def _load_performance(
    conn: DbConnection, query: DiagnosticQuery, value_range: DiagnosticRange
) -> _PerformanceData:
    sql_filter = _observation_filter(query, value_range)
    return _PerformanceData(
        call_count=_count(conn, sql_filter),
        latency=_latency_stats(conn, sql_filter),
        response_sizes=_response_size_stats(conn, sql_filter),
        by_tool=_grouped_call_metrics(conn, sql_filter, "tool"),
        slowest=_select_observations(conn, sql_filter, query.limit, query.offset, "slowest"),
        largest=_select_observations(conn, sql_filter, query.limit, query.offset, "largest"),
    )


@dataclass(frozen=True)
class _DatabaseRead[T]:
    value: T | None
    degradations: tuple[DiagnosticDegradation, ...]


@dataclass(frozen=True)
class _RuntimeRead:
    batch: RuntimeEventBatch
    degradations: tuple[DiagnosticDegradation, ...]


def _runtime_degradations(batch: RuntimeEventBatch) -> tuple[DiagnosticDegradation, ...]:
    items: list[DiagnosticDegradation] = []
    if not batch.log_available:
        items.append(
            _degradation(
                "runtime_log",
                "runtime_log_missing",
                "The private runtime event log does not exist yet.",
            )
        )
    if batch.malformed_lines:
        items.append(
            _degradation(
                "runtime_log",
                "runtime_log_malformed",
                "Malformed runtime event records were skipped.",
                batch.malformed_lines,
            )
        )
    if batch.oversized_lines:
        items.append(
            _degradation(
                "runtime_log",
                "runtime_log_oversized",
                "Oversized runtime event records were skipped.",
                batch.oversized_lines,
            )
        )
    return tuple(items)


def _read_runtime(
    workspace: WorkspaceRoot,
    limit: int,
    value_range: DiagnosticRange | None = None,
    correlation_id: str | None = None,
    event_filter: RuntimeEventFilter = "all",
) -> _RuntimeRead:
    try:
        batch = read_runtime_events(
            workspace,
            RuntimeEventQuery(
                limit=max(1, min(limit, MAX_RUNTIME_EVENT_LIMIT)),
                correlation_id=correlation_id,
                start=None if value_range is None else value_range.start,
                end=None if value_range is None else value_range.end,
                event_filter=event_filter,
            ),
        )
    except (OSError, PathSafetyError, ValueError):
        degradation = _degradation(
            "runtime_log",
            "runtime_log_unavailable",
            "The private runtime event log could not be opened safely.",
        )
        return _RuntimeRead(RuntimeEventBatch(log_available=False, events=()), (degradation,))
    return _RuntimeRead(batch, _runtime_degradations(batch))


def _read_lifecycle_history(workspace: WorkspaceRoot) -> _RuntimeRead:
    """Read lifecycle records independently of display windows and event limits."""

    return _read_runtime(
        workspace,
        MAX_RUNTIME_EVENT_LIMIT,
        event_filter="lifecycle",
    )


def _related_metadata(
    members: Sequence[InternalErrorEvent],
    observations: Mapping[str, DiagnosticObservation],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    related = [
        observations[item.correlation_id] for item in members if item.correlation_id in observations
    ]
    tools = tuple(sorted({item.tool_name for item in related}))
    projects = tuple(sorted({item.project_key for item in related if item.project_key is not None}))
    return tools, projects


def _runtime_error_group(
    fingerprint: str,
    members: Sequence[InternalErrorEvent],
    observations: Mapping[str, DiagnosticObservation],
) -> RuntimeErrorGroup:
    ordered = sorted(members, key=lambda item: item.timestamp)
    tools, projects = _related_metadata(ordered, observations)
    return RuntimeErrorGroup(
        stack_fingerprint=fingerprint,
        exception_type=ordered[-1].exception_type,
        count=len(ordered),
        first_seen=ordered[0].timestamp,
        last_seen=ordered[-1].timestamp,
        correlation_ids=tuple(dict.fromkeys(item.correlation_id for item in reversed(ordered)))[
            :100
        ],
        affected_tools=tools,
        affected_projects=projects,
    )


def _runtime_error_groups(
    events: Sequence[RuntimeEvent],
    observations: Mapping[str, DiagnosticObservation] | None = None,
) -> tuple[RuntimeErrorGroup, ...]:
    grouped: dict[str, list[InternalErrorEvent]] = {}
    for event in events:
        if isinstance(event, InternalErrorEvent):
            grouped.setdefault(event.stack_fingerprint, []).append(event)
    observation_map = observations or {}
    groups = [
        _runtime_error_group(fingerprint, members, observation_map)
        for fingerprint, members in grouped.items()
    ]
    return tuple(sorted(groups, key=lambda item: item.last_seen, reverse=True))


def _filtered_internal_events(
    events: Sequence[RuntimeEvent],
    query: DiagnosticQuery,
    observations: Sequence[DiagnosticObservation],
) -> tuple[InternalErrorEvent, ...]:
    """Apply observation-backed filters without guessing missing-row metadata."""

    status = _effective_status(query)
    if status in {"success", "unknown"}:
        return ()
    if query.error_code not in {None, "INTERNAL_ERROR"}:
        return ()
    internal = tuple(event for event in events if isinstance(event, InternalErrorEvent))
    if query.project is None and query.tool is None and query.client is None:
        return internal
    correlation_ids = {observation.correlation_id for observation in observations}
    return tuple(event for event in internal if event.correlation_id in correlation_ids)


def _filtered_write_failures(
    events: Sequence[RuntimeEvent],
    query: DiagnosticQuery,
) -> tuple[ObservationWriteFailedEvent, ...]:
    """Filter only on fields a missing observation can prove; never guess scope."""

    status = _effective_status(query)
    if status in {"success", "unknown"}:
        return ()
    if query.project is not None or query.client is not None or query.error_code is not None:
        return ()
    return tuple(
        event
        for event in events
        if isinstance(event, ObservationWriteFailedEvent)
        and (query.tool is None or event.tool_name == query.tool)
    )


def _prior_boots_without_stop(events: Sequence[RuntimeEvent]) -> int:
    starts = {
        event.server_boot_id: event.timestamp
        for event in events
        if isinstance(event, ProcessStartedEvent)
    }
    stops = {event.server_boot_id for event in events if isinstance(event, ProcessStoppingEvent)}
    if not starts:
        return 0
    latest_boot = max(starts, key=starts.__getitem__)
    return sum(boot != latest_boot and boot not in stops for boot in starts)


def _lifecycle_event_state(
    event: RuntimeEvent,
) -> tuple[LifecycleStatus, LifecycleCertainty, str]:
    states: dict[str, tuple[LifecycleStatus, LifecycleCertainty, str]] = {
        "process_started": ("started", "observed", "The latest process start was recorded."),
        "client_initialized": (
            "client_initialized",
            "observed",
            "A client initialized against the latest known process.",
        ),
        "transport_closed": (
            "transport_closed",
            "observed",
            "The latest known transport closed; process liveness is unknown.",
        ),
        "process_stopping": (
            "stopped_cleanly",
            "observed",
            "The latest known process entered its normal stop path.",
        ),
    }
    return states[event.event]


def _infer_lifecycle(
    events: Sequence[RuntimeEvent],
    latest_observation: DiagnosticObservation | None,
    log_available: bool,
) -> LifecycleState:
    lifecycle_events = [
        event
        for event in events
        if event.event
        in {"process_started", "client_initialized", "transport_closed", "process_stopping"}
    ]
    latest_event = max(lifecycle_events, key=lambda item: item.timestamp, default=None)
    prior_unclosed = _prior_boots_without_stop(events)
    if latest_observation is not None and (
        latest_event is None or latest_observation.created_at > latest_event.timestamp
    ):
        return LifecycleState(
            status="activity_observed",
            certainty="observed",
            description="MCP activity was observed; current process liveness is not asserted.",
            server_boot_id=latest_observation.server_boot_id,
            process_id=latest_observation.process_id,
            observed_at=latest_observation.created_at,
            latest_observation_at=latest_observation.created_at,
            prior_boots_without_clean_stop=prior_unclosed,
        )
    if latest_event is not None:
        status, certainty, description = _lifecycle_event_state(latest_event)
        return LifecycleState(
            status=status,
            certainty=certainty,
            description=description,
            server_boot_id=latest_event.server_boot_id,
            process_id=latest_event.process_id,
            observed_at=latest_event.timestamp,
            latest_observation_at=(
                None if latest_observation is None else latest_observation.created_at
            ),
            prior_boots_without_clean_stop=prior_unclosed,
        )
    if latest_observation is not None:
        return LifecycleState(
            status="activity_observed",
            certainty="observed",
            description="MCP activity exists without corresponding lifecycle records.",
            server_boot_id=latest_observation.server_boot_id,
            process_id=latest_observation.process_id,
            observed_at=latest_observation.created_at,
            latest_observation_at=latest_observation.created_at,
        )
    if log_available:
        return LifecycleState(
            status="never_started",
            certainty="inferred",
            description="No process start or MCP activity has been recorded.",
        )
    return LifecycleState(
        status="unknown",
        certainty="unknown",
        description="Lifecycle state is unavailable because no diagnostic history can be read.",
    )


def _observation_mapping(
    observations: Sequence[DiagnosticObservation],
) -> dict[str, DiagnosticObservation]:
    return {item.correlation_id: item for item in observations}


def _load_latest_observation(conn: DbConnection) -> DiagnosticObservation | None:
    rows = conn.execute(
        """SELECT * FROM mcp_call_observations
           ORDER BY created_at DESC, id DESC LIMIT 1"""
    ).fetchall()
    observations = _observations(rows)
    return observations[0] if observations else None


def _empty_summary() -> CountSummary:
    return CountSummary(calls=0, failures=0, failure_rate=0.0, internal_errors=0)


def _empty_latency() -> LatencyStats:
    return LatencyStats(sample_count=0)


def _empty_response_sizes() -> ResponseSizeStats:
    return ResponseSizeStats(sample_count=0)


def _empty_activity(value_range: DiagnosticRange) -> tuple[ActivityBucket, ...]:
    bucket_seconds, bucket_count = _bucket_plan(value_range)
    result: list[ActivityBucket] = []
    for index in range(bucket_count):
        start = value_range.start + timedelta(seconds=index * bucket_seconds)
        end = min(start + timedelta(seconds=bucket_seconds), value_range.end)
        result.append(ActivityBucket(start=start, end=end, calls=0, failures=0))
    return tuple(result)


class DiagnosticsService:
    """Read-only facade shared by operator presentation layers."""

    def __init__(self, workspace: WorkspaceRoot) -> None:
        self._workspace = workspace

    def _database_path(self) -> Path:
        return contained_path(self._workspace, ".ferumind/ferumind.sqlite")

    def _read_database[T](self, operation: Callable[[DbConnection], T]) -> _DatabaseRead[T]:
        try:
            path = self._database_path()
        except (OSError, PathSafetyError):
            item = _degradation(
                "database",
                "database_unavailable",
                "The observation database path could not be resolved safely.",
            )
            return _DatabaseRead(None, (item,))
        if not path.is_file():
            item = _degradation(
                "database",
                "database_missing",
                "The observation database does not exist yet.",
            )
            return _DatabaseRead(None, (item,))
        try:
            conn = Database(path).get_readonly_connection()
        except (OSError, sqlite3.Error):
            item = _degradation(
                "database",
                "database_unavailable",
                "The observation database could not be opened read-only.",
            )
            return _DatabaseRead(None, (item,))
        try:
            value = operation(conn)
        except (KeyError, OSError, TypeError, ValueError, sqlite3.Error):
            item = _degradation(
                "database",
                "database_unavailable",
                "The observation database could not answer the diagnostic query.",
            )
            return _DatabaseRead(None, (item,))
        finally:
            with suppress(sqlite3.Error):
                conn.close()
        return _DatabaseRead(value, ())

    def _database_size(self) -> int | None:
        try:
            return self._database_path().stat().st_size
        except (OSError, PathSafetyError):
            return None

    def _workspace_degradations(self) -> tuple[DiagnosticDegradation, ...]:
        try:
            available = Path(self._workspace).is_dir()
        except OSError:
            available = False
        if available:
            return ()
        item = _degradation(
            "workspace",
            "workspace_missing",
            "The configured workspace is unavailable.",
        )
        return (item,)

    def meta(self) -> DiagnosticsMeta:
        """Return storage availability and durable observation metadata."""

        generated_at = _now()
        database = self._read_database(_load_meta)
        runtime = _read_runtime(self._workspace, 1)
        data = database.value or _MetaData(0, None)
        workspace_degradations = self._workspace_degradations()
        return DiagnosticsMeta(
            generated_at=generated_at,
            workspace_available=not workspace_degradations,
            database_available=database.value is not None,
            runtime_log_available=runtime.batch.log_available,
            database_size_bytes=(self._database_size() if database.value is not None else None),
            observation_count=data.observation_count,
            latest_observation_at=data.latest_observation_at,
            degradations=_merge_degradations(
                workspace_degradations, database.degradations, runtime.degradations
            ),
        )

    def overview(self, query: DiagnosticQuery) -> OverviewReport:
        """Return the selected period's activity, summary, and latest state."""

        generated_at = _now()
        value_range = _resolved_range(query, generated_at)
        database = self._read_database(lambda conn: _load_overview(conn, query, value_range))
        runtime = _read_runtime(self._workspace, MAX_RUNTIME_EVENT_LIMIT, value_range)
        lifecycle_history = _read_lifecycle_history(self._workspace)
        data = database.value
        latest = None if data is None else data.latest_observation
        events = runtime.batch.events
        return OverviewReport(
            generated_at=generated_at,
            range=value_range,
            summary=_empty_summary() if data is None else data.summary,
            latency=_empty_latency() if data is None else data.latency,
            response_sizes=_empty_response_sizes() if data is None else data.response_sizes,
            activity=_empty_activity(value_range) if data is None else data.activity,
            latest_observation=latest,
            latest_failure=None if data is None else data.latest_failure,
            latest_client=None if data is None else data.latest_client,
            observation_write_failures=sum(
                isinstance(event, ObservationWriteFailedEvent) for event in events
            ),
            latest_runtime_event=events[0] if events else None,
            lifecycle=_infer_lifecycle(
                lifecycle_history.batch.events,
                latest,
                lifecycle_history.batch.log_available,
            ),
            degradations=_merge_degradations(
                database.degradations,
                runtime.degradations,
                lifecycle_history.degradations,
            ),
        )

    def calls(self, query: DiagnosticQuery) -> CallsReport:
        """Return a bounded page of calls plus safe grouped facets."""

        generated_at = _now()
        value_range = _resolved_range(query, generated_at)
        database = self._read_database(lambda conn: _load_calls(conn, query, value_range))
        data = database.value
        observations = () if data is None else data.observations
        total = 0 if data is None else data.total
        return CallsReport(
            generated_at=generated_at,
            range=value_range,
            total=total,
            returned=len(observations),
            has_more=query.offset + len(observations) < total,
            observations=observations,
            grouped_by_tool=() if data is None else data.by_tool,
            grouped_by_project=() if data is None else data.by_project,
            grouped_by_client=() if data is None else data.by_client,
            degradations=database.degradations,
        )

    def errors(self, query: DiagnosticQuery) -> ErrorsReport:
        """Return expected failures, correlated internal bugs, and telemetry failures."""

        generated_at = _now()
        value_range = _resolved_range(query, generated_at)
        database = self._read_database(lambda conn: _load_errors(conn, query, value_range))
        runtime = _read_runtime(self._workspace, MAX_RUNTIME_EVENT_LIMIT, value_range)
        data = database.value
        recent = () if data is None else data.recent
        group_observations = () if data is None else data.internal_group_observations
        runtime_events = runtime.batch.events
        internal_events = _filtered_internal_events(runtime_events, query, group_observations)
        return ErrorsReport(
            generated_at=generated_at,
            range=value_range,
            failure_count=0 if data is None else data.failure_count,
            error_code_groups=() if data is None else data.groups,
            internal_error_groups=_runtime_error_groups(
                internal_events,
                _observation_mapping(group_observations),
            ),
            recent_failures=recent,
            recent_internal_errors=() if data is None else data.recent_internal,
            observation_write_failures=_filtered_write_failures(runtime_events, query),
            degradations=_merge_degradations(database.degradations, runtime.degradations),
        )

    def performance(self, query: DiagnosticQuery) -> PerformanceReport:
        """Return exact bounded percentile summaries and diagnostic outliers."""

        generated_at = _now()
        value_range = _resolved_range(query, generated_at)
        database = self._read_database(lambda conn: _load_performance(conn, query, value_range))
        data = database.value
        return PerformanceReport(
            generated_at=generated_at,
            range=value_range,
            call_count=0 if data is None else data.call_count,
            latency=_empty_latency() if data is None else data.latency,
            response_sizes=_empty_response_sizes() if data is None else data.response_sizes,
            calls_by_tool=() if data is None else data.by_tool,
            slowest_calls=() if data is None else data.slowest,
            largest_responses=() if data is None else data.largest,
            degradations=database.degradations,
        )

    def runtime(self, query: DiagnosticQuery | None = None) -> RuntimeReport:
        """Return windowed safe events plus independently inferred lifecycle."""

        generated_at = _now()
        runtime_query = query or DiagnosticQuery(limit=DEFAULT_RUNTIME_LIMIT)
        value_range = _resolved_range(runtime_query, generated_at)
        runtime = _read_runtime(self._workspace, runtime_query.limit, value_range)
        lifecycle_history = _read_lifecycle_history(self._workspace)
        database = self._read_database(_load_latest_observation)
        events = runtime.batch.events
        return RuntimeReport(
            generated_at=generated_at,
            log_available=runtime.batch.log_available,
            events=events,
            internal_error_groups=_runtime_error_groups(events),
            lifecycle=_infer_lifecycle(
                lifecycle_history.batch.events,
                database.value,
                lifecycle_history.batch.log_available,
            ),
            malformed_lines=runtime.batch.malformed_lines,
            oversized_lines=runtime.batch.oversized_lines,
            degradations=_merge_degradations(
                runtime.degradations,
                lifecycle_history.degradations,
                database.degradations,
            ),
        )

    def observation(self, correlation_id: str) -> ObservationDetailReport:
        """Combine one exact opaque correlation lookup with safe runtime details."""

        generated_at = _now()
        database = self._read_database(
            lambda conn: _lookup_diagnostic_observation(conn, correlation_id)
        )
        runtime = _read_runtime(
            self._workspace,
            MAX_RUNTIME_EVENT_LIMIT,
            correlation_id=correlation_id,
        )
        events = runtime.batch.events
        internal = next((event for event in events if isinstance(event, InternalErrorEvent)), None)
        return ObservationDetailReport(
            generated_at=generated_at,
            correlation_id=correlation_id,
            found=database.value is not None or bool(events),
            observation=database.value,
            runtime_events=events,
            safe_frames=() if internal is None else internal.frames,
            degradations=_merge_degradations(database.degradations, runtime.degradations),
        )

    def doctor(self) -> DoctorReport:
        """Return a compact degraded-state-aware operator diagnosis."""

        generated_at = _now()
        meta = self.meta()
        hour = self.overview(DiagnosticQuery(window="1h", limit=5))
        day = self.overview(DiagnosticQuery(window="24h", limit=5))
        errors = self.errors(DiagnosticQuery(window="24h", limit=20))
        performance = self.performance(DiagnosticQuery(window="24h", limit=5))
        degradations = _merge_degradations(
            meta.degradations,
            hour.degradations,
            day.degradations,
            errors.degradations,
            performance.degradations,
        )
        return DoctorReport(
            generated_at=generated_at,
            meta=meta,
            last_hour=hour.summary,
            last_24_hours=day.summary,
            error_code_groups=errors.error_code_groups,
            observation_write_failure_count=len(errors.observation_write_failures),
            lifecycle=day.lifecycle,
            slowest_calls=performance.slowest_calls,
            largest_responses=performance.largest_responses,
            degradations=degradations,
        )


def _lookup_diagnostic_observation(
    conn: DbConnection, correlation_id: str
) -> DiagnosticObservation | None:
    record = get_observation_by_correlation_id(conn, correlation_id)
    return None if record is None else _diagnostic_observation(record)
