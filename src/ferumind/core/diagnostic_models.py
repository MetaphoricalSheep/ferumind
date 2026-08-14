"""Typed, presentation-neutral models for local operator diagnostics.

The dashboard and CLI serialize these models directly.  They intentionally
contain only metadata already admitted by the observation/runtime privacy
boundaries; no request or response payload has a field to leak through.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from ferumind.core.runtime_events import RuntimeEvent, SafeStackFrame
from ferumind.core.types import JsonObject, StrictModel

type DiagnosticWindow = Literal["1h", "24h", "7d", "30d"]
type ObservationStatus = Literal["all", "success", "failed", "unknown"]
type DiagnosticComponent = Literal["workspace", "database", "runtime_log"]
type DegradationCode = Literal[
    "workspace_missing",
    "database_missing",
    "database_unavailable",
    "runtime_log_missing",
    "runtime_log_unavailable",
    "runtime_log_malformed",
    "runtime_log_oversized",
]
type LifecycleStatus = Literal[
    "unknown",
    "never_started",
    "started",
    "client_initialized",
    "activity_observed",
    "transport_closed",
    "stopped_cleanly",
]
type LifecycleCertainty = Literal["observed", "inferred", "unknown"]


class DiagnosticQuery(StrictModel):
    """Validated filters shared by CLI and dashboard query surfaces."""

    window: DiagnosticWindow = "24h"
    start: datetime | None = None
    end: datetime | None = None
    project: str | None = Field(default=None, max_length=256)
    tool: str | None = Field(default=None, max_length=256)
    client: str | None = Field(default=None, max_length=256)
    error_code: str | None = Field(default=None, max_length=256)
    status: ObservationStatus = "all"
    failed: bool | None = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0, le=100_000)

    @model_validator(mode="after")
    def validate_range_and_status(self) -> Self:
        for value in (self.start, self.end):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Diagnostic range timestamps must include a timezone")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("Diagnostic range start must be earlier than end")
        if self.failed is True and self.status not in {"all", "failed"}:
            raise ValueError("failed=true conflicts with the selected status")
        if self.failed is False and self.status not in {"all", "success"}:
            raise ValueError("failed=false conflicts with the selected status")
        return self


class DiagnosticRange(StrictModel):
    start: datetime
    end: datetime


class DiagnosticDegradation(StrictModel):
    component: DiagnosticComponent
    code: DegradationCode
    message: str
    affected_records: int | None = Field(default=None, ge=0)


class DiagnosticObservation(StrictModel):
    """One observation with its bounded JSON metadata safely decoded."""

    id: str
    correlation_id: str
    tool_name: str
    project_key: str | None = None
    created_at: datetime
    ok: bool | None = None
    error_code: str | None = None
    transport: str | None = None
    server_boot_id: str
    process_id: int
    client_name: str | None = None
    client_version: str | None = None
    protocol_version: str | None = None
    duration_ms: float | None = None
    result_bytes: int | None = None
    context_metrics: JsonObject = Field(default_factory=dict)
    argument_keys: tuple[str, ...] = ()
    redaction_notes: tuple[str, ...] = ()
    metadata_degraded: bool = False


class CountSummary(StrictModel):
    calls: int = Field(ge=0)
    failures: int = Field(ge=0)
    failure_rate: float = Field(ge=0.0, le=1.0)
    internal_errors: int = Field(ge=0)


class LatencyStats(StrictModel):
    sample_count: int = Field(ge=0)
    p50_ms: float | None = None
    p95_ms: float | None = None
    max_ms: float | None = None


class ResponseSizeStats(StrictModel):
    sample_count: int = Field(ge=0)
    p50_bytes: float | None = None
    p95_bytes: float | None = None
    max_bytes: int | None = None


class GroupedCallMetrics(StrictModel):
    dimension: str | None = None
    calls: int = Field(ge=0)
    failures: int = Field(ge=0)
    failure_rate: float = Field(ge=0.0, le=1.0)
    latency: LatencyStats


class ActivityBucket(StrictModel):
    start: datetime
    end: datetime
    calls: int = Field(ge=0)
    failures: int = Field(ge=0)


class ErrorCodeGroup(StrictModel):
    error_code: str | None = None
    count: int = Field(ge=1)
    first_seen: datetime
    last_seen: datetime
    affected_tools: tuple[str, ...] = ()
    affected_projects: tuple[str, ...] = ()


class RuntimeErrorGroup(StrictModel):
    stack_fingerprint: str
    exception_type: str
    count: int = Field(ge=1)
    first_seen: datetime
    last_seen: datetime
    correlation_ids: tuple[str, ...]
    affected_tools: tuple[str, ...] = ()
    affected_projects: tuple[str, ...] = ()


class LifecycleState(StrictModel):
    status: LifecycleStatus
    certainty: LifecycleCertainty
    description: str
    server_boot_id: str | None = None
    process_id: int | None = None
    observed_at: datetime | None = None
    latest_observation_at: datetime | None = None
    prior_boots_without_clean_stop: int = Field(default=0, ge=0)


class DiagnosticsMeta(StrictModel):
    generated_at: datetime
    workspace_available: bool
    database_available: bool
    runtime_log_available: bool
    database_size_bytes: int | None = Field(default=None, ge=0)
    observation_count: int = Field(default=0, ge=0)
    latest_observation_at: datetime | None = None
    supported_windows: tuple[DiagnosticWindow, ...] = ("1h", "24h", "7d", "30d")
    degradations: tuple[DiagnosticDegradation, ...] = ()


class OverviewReport(StrictModel):
    generated_at: datetime
    range: DiagnosticRange
    summary: CountSummary
    latency: LatencyStats
    response_sizes: ResponseSizeStats
    activity: tuple[ActivityBucket, ...]
    latest_observation: DiagnosticObservation | None = None
    latest_failure: DiagnosticObservation | None = None
    latest_client: str | None = None
    observation_write_failures: int = Field(default=0, ge=0)
    latest_runtime_event: RuntimeEvent | None = None
    lifecycle: LifecycleState
    degradations: tuple[DiagnosticDegradation, ...] = ()


class CallsReport(StrictModel):
    generated_at: datetime
    range: DiagnosticRange
    total: int = Field(ge=0)
    returned: int = Field(ge=0)
    has_more: bool
    observations: tuple[DiagnosticObservation, ...]
    grouped_by_tool: tuple[GroupedCallMetrics, ...] = ()
    grouped_by_project: tuple[GroupedCallMetrics, ...] = ()
    grouped_by_client: tuple[GroupedCallMetrics, ...] = ()
    degradations: tuple[DiagnosticDegradation, ...] = ()


class ErrorsReport(StrictModel):
    generated_at: datetime
    range: DiagnosticRange
    failure_count: int = Field(ge=0)
    error_code_groups: tuple[ErrorCodeGroup, ...]
    internal_error_groups: tuple[RuntimeErrorGroup, ...]
    recent_failures: tuple[DiagnosticObservation, ...]
    recent_internal_errors: tuple[DiagnosticObservation, ...]
    observation_write_failures: tuple[RuntimeEvent, ...]
    degradations: tuple[DiagnosticDegradation, ...] = ()


class PerformanceReport(StrictModel):
    generated_at: datetime
    range: DiagnosticRange
    call_count: int = Field(ge=0)
    latency: LatencyStats
    response_sizes: ResponseSizeStats
    calls_by_tool: tuple[GroupedCallMetrics, ...]
    slowest_calls: tuple[DiagnosticObservation, ...]
    largest_responses: tuple[DiagnosticObservation, ...]
    degradations: tuple[DiagnosticDegradation, ...] = ()


class RuntimeReport(StrictModel):
    generated_at: datetime
    log_available: bool
    events: tuple[RuntimeEvent, ...]
    internal_error_groups: tuple[RuntimeErrorGroup, ...]
    lifecycle: LifecycleState
    malformed_lines: int = Field(default=0, ge=0)
    oversized_lines: int = Field(default=0, ge=0)
    degradations: tuple[DiagnosticDegradation, ...] = ()


class ObservationDetailReport(StrictModel):
    generated_at: datetime
    correlation_id: str
    found: bool
    observation: DiagnosticObservation | None = None
    runtime_events: tuple[RuntimeEvent, ...] = ()
    safe_frames: tuple[SafeStackFrame, ...] = ()
    degradations: tuple[DiagnosticDegradation, ...] = ()


class DoctorReport(StrictModel):
    generated_at: datetime
    meta: DiagnosticsMeta
    last_hour: CountSummary
    last_24_hours: CountSummary
    error_code_groups: tuple[ErrorCodeGroup, ...]
    observation_write_failure_count: int = Field(ge=0)
    lifecycle: LifecycleState
    slowest_calls: tuple[DiagnosticObservation, ...]
    largest_responses: tuple[DiagnosticObservation, ...]
    degradations: tuple[DiagnosticDegradation, ...] = ()
