"""Operator diagnostic, observation, and dashboard CLI commands."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer
from pydantic import BaseModel, ValidationError
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ferumind.cli.common import WORKSPACE_OPTION, workspace_root
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.runtime_events import InternalErrorEvent, ObservationWriteFailedEvent

if TYPE_CHECKING:
    from ferumind.core.diagnostic_models import (
        CallsReport,
        DiagnosticObservation,
        DiagnosticQuery,
        DiagnosticWindow,
        DoctorReport,
        ErrorsReport,
        ObservationDetailReport,
    )
    from ferumind.core.diagnostics import DiagnosticsService

observations_app = typer.Typer(
    help="Inspect metadata-only MCP observations by filter or correlation ID.",
    no_args_is_help=True,
)

_WINDOWS = frozenset({"1h", "24h", "7d", "30d"})
_WINDOW_DELTAS = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


@dataclass(frozen=True)
class _ObservationScope:
    workspace: Path | None
    project: str | None
    tool: str | None
    client: str | None


class _ScopedLimit(int):
    """A validated CLI limit carrying its invocation-local parent scope."""

    scope: _ObservationScope

    def __new__(cls, value: int, scope: _ObservationScope) -> _ScopedLimit:
        instance = super().__new__(cls, value)
        instance.scope = scope
        return instance


@dataclass(frozen=True)
class _TimeOptions:
    since: str | None
    until: str | None


def _scope_from_context(ctx: typer.Context) -> _ObservationScope:
    value = ctx.parent.obj if ctx.parent is not None else None
    if not isinstance(value, _ObservationScope):
        raise RuntimeError("Observation command scope was not initialized")
    return value


def _limit_with_scope(ctx: typer.Context, value: int) -> _ScopedLimit:
    return _ScopedLimit(value, _scope_from_context(ctx))


def _scope_from_limit(limit: int) -> _ObservationScope:
    if not isinstance(limit, _ScopedLimit):
        raise RuntimeError("Observation command limit was not initialized")
    return limit.scope


def _service(workspace: Path | None) -> DiagnosticsService:
    from ferumind.core.diagnostics import DiagnosticsService

    return DiagnosticsService(WorkspaceRoot(workspace_root(workspace)))


def _echo_json(model: BaseModel) -> None:
    typer.echo(json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2))


def _safe_text(value: object, *, fallback: str = "—") -> str:
    if value is None:
        return fallback
    raw = str(value)
    cleaned = "".join(character if character.isprintable() else "�" for character in raw)
    return cleaned[:512] or fallback


def _literal_text(value: object, *, fallback: str = "—") -> Text:
    """Render persisted metadata literally instead of as Rich markup."""

    return Text(_safe_text(value, fallback=fallback))


def _format_time(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ") if value else "—"


def _format_bytes(value: int | float | None) -> str:
    if value is None:
        return "—"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(amount) < 1024.0 or unit == "GiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024.0
    return "—"


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise typer.BadParameter("must be an ISO-8601 timestamp with a timezone") from exc
    if parsed.utcoffset() is None:
        raise typer.BadParameter("must include a timezone")
    return parsed.astimezone(UTC)


def _time_bounds(
    options: _TimeOptions,
) -> tuple[DiagnosticWindow, datetime | None, datetime | None]:
    window = cast("DiagnosticWindow", options.since if options.since in _WINDOWS else "24h")
    if options.since in _WINDOWS:
        end = _parse_timestamp(options.until) if options.until else datetime.now(UTC)
        return window, end - _WINDOW_DELTAS[options.since], end
    start = _parse_timestamp(options.since) if options.since else None
    end = _parse_timestamp(options.until) if options.until else None
    if start is not None and end is None:
        end = datetime.now(UTC)
    if start is None and end is not None:
        start = end - _WINDOW_DELTAS["24h"]
    return window, start, end


def _query(
    scope: _ObservationScope,
    time_options: _TimeOptions,
    *,
    failed: bool | None,
    limit: int,
    error_code: str | None = None,
) -> DiagnosticQuery:
    from ferumind.core.diagnostic_models import DiagnosticQuery

    window, start, end = _time_bounds(time_options)
    try:
        return DiagnosticQuery(
            window=window,
            start=start,
            end=end,
            project=scope.project,
            tool=scope.tool,
            client=scope.client,
            error_code=error_code,
            failed=failed,
            limit=limit,
        )
    except ValidationError as exc:
        raise typer.BadParameter("diagnostic filters are invalid") from exc


def _observation_table(
    observations: tuple[DiagnosticObservation, ...],
    *,
    title: str,
) -> Table:
    table = Table(title=title)
    for heading in ("Time", "Status", "Tool", "Project", "Client", "Duration", "Bytes", "Error"):
        table.add_column(heading)
    for item in observations:
        status = "success" if item.ok is True else "failed" if item.ok is False else "unknown"
        client = item.client_name or "Not exposed"
        table.add_row(
            _format_time(item.created_at),
            status,
            _literal_text(item.tool_name),
            _literal_text(item.project_key),
            _literal_text(client),
            f"{item.duration_ms:.1f} ms" if item.duration_ms is not None else "—",
            _format_bytes(item.result_bytes),
            _literal_text(item.error_code),
        )
    return table


def _write_calls(report: CallsReport) -> None:
    typer.echo(
        f"{report.total} matching observation(s); returned {report.returned}"
        f"{' (more available)' if report.has_more else ''}."
    )
    Console().print(_observation_table(report.observations, title="MCP observations"))
    for degradation in report.degradations:
        typer.echo(f"Degraded: {_safe_text(degradation.message)}", err=True)


def _write_errors(report: ErrorsReport) -> None:
    groups = Table(title="Failures by error code")
    for heading in ("Error code", "Count", "First seen", "Last seen", "Tools", "Projects"):
        groups.add_column(heading)
    for group in report.error_code_groups:
        groups.add_row(
            _literal_text(group.error_code, fallback="Unknown"),
            str(group.count),
            _format_time(group.first_seen),
            _format_time(group.last_seen),
            _literal_text(", ".join(group.affected_tools)),
            _literal_text(", ".join(group.affected_projects)),
        )
    console = Console()
    console.print(groups)
    console.print(_observation_table(report.recent_failures, title="Recent failures"))
    typer.echo(
        f"Internal fingerprints: {len(report.internal_error_groups)}; "
        f"observation-write failures: {len(report.observation_write_failures)}"
    )
    for degradation in report.degradations:
        typer.echo(f"Degraded: {_safe_text(degradation.message)}", err=True)


def _write_runtime_detail(report: ObservationDetailReport) -> None:
    typer.echo(f"Correlated runtime events: {len(report.runtime_events)}")
    for event in report.runtime_events:
        typer.echo(f"  {_format_time(event.timestamp)} {_safe_text(event.event)}")
        if isinstance(event, InternalErrorEvent):
            typer.echo(f"    Exception type: {_safe_text(event.exception_type)}")
            typer.echo(f"    Stack fingerprint: {_safe_text(event.stack_fingerprint)}")
        elif isinstance(event, ObservationWriteFailedEvent):
            typer.echo(
                f"    Tool / stage / type: {_safe_text(event.tool_name)} / "
                f"{event.stage} / {_safe_text(event.exception_type)}"
            )
    for frame in report.safe_frames:
        typer.echo(
            f"  {_safe_text(frame.module)}:{_safe_text(frame.source_path)}:"
            f"{frame.line} in {_safe_text(frame.function)}"
        )


def _write_detail(report: ObservationDetailReport) -> None:
    for degradation in report.degradations:
        typer.echo(f"Degraded: {_safe_text(degradation.message)}", err=True)
    if not report.found:
        typer.echo(f"No observation found for correlation ID {_safe_text(report.correlation_id)}.")
        raise typer.Exit(code=1)
    if report.observation is None:
        typer.echo(f"Correlation ID: {_safe_text(report.correlation_id)}")
        typer.echo("SQLite observation unavailable; showing correlated runtime diagnostics.")
        _write_runtime_detail(report)
        return
    observation = report.observation
    typer.echo(f"Correlation ID: {_safe_text(observation.correlation_id)}")
    typer.echo(f"Observation ID: {_safe_text(observation.id)}")
    typer.echo(f"Time: {_format_time(observation.created_at)}")
    typer.echo(f"Tool: {_safe_text(observation.tool_name)}")
    typer.echo(f"Project: {_safe_text(observation.project_key)}")
    status = (
        "success" if observation.ok is True else "failed" if observation.ok is False else "unknown"
    )
    typer.echo(f"Status: {status}")
    typer.echo(f"Error code: {_safe_text(observation.error_code)}")
    typer.echo(
        f"Client: {_safe_text(observation.client_name)} {_safe_text(observation.client_version)}"
    )
    typer.echo(f"Protocol: {_safe_text(observation.protocol_version)}")
    typer.echo(f"Transport: {_safe_text(observation.transport)}")
    typer.echo(
        f"Server boot / PID: {_safe_text(observation.server_boot_id)} / {observation.process_id}"
    )
    duration = f"{observation.duration_ms:.1f} ms" if observation.duration_ms is not None else "—"
    typer.echo(f"Duration / result: {duration} / {_format_bytes(observation.result_bytes)}")
    typer.echo(f"Argument keys: {_safe_text(', '.join(observation.argument_keys))}")
    typer.echo(
        f"Safe context metrics: {json.dumps(observation.context_metrics, ensure_ascii=False)}"
    )
    _write_runtime_detail(report)


def _write_doctor(report: DoctorReport) -> None:
    meta = report.meta
    typer.echo("Ferumind doctor")
    typer.echo(f"Workspace: {'available' if meta.workspace_available else 'unavailable'}")
    typer.echo(
        f"Database: {'available' if meta.database_available else 'unavailable'}; "
        f"{_format_bytes(meta.database_size_bytes)}; {meta.observation_count} observation(s)"
    )
    typer.echo(f"Latest observation: {_format_time(meta.latest_observation_at)}")
    typer.echo(f"Last hour: {report.last_hour.calls} call(s)")
    typer.echo(
        f"Last 24 hours: {report.last_24_hours.calls} call(s), "
        f"{report.last_24_hours.failures} failure(s), "
        f"{report.last_24_hours.failure_rate:.1%} failure rate, "
        f"{report.last_24_hours.internal_errors} INTERNAL_ERROR"
    )
    codes = ", ".join(
        f"{group.error_code or 'unknown'}={group.count}" for group in report.error_code_groups
    )
    typer.echo(f"Error codes: {codes or '(none)'}")
    typer.echo(f"Observation-persistence failures: {report.observation_write_failure_count}")
    typer.echo(
        f"Lifecycle: {report.lifecycle.status} ({report.lifecycle.certainty}) — "
        f"{_safe_text(report.lifecycle.description)}"
    )
    Console().print(_observation_table(report.slowest_calls, title="Slowest recent calls"))
    Console().print(_observation_table(report.largest_responses, title="Largest recent responses"))
    for degradation in report.degradations:
        typer.echo(f"Degraded: {_safe_text(degradation.message)}", err=True)


@observations_app.callback()
def observation_scope(
    ctx: typer.Context,
    workspace: Annotated[Path | None, WORKSPACE_OPTION] = None,
    project: Annotated[str | None, typer.Option("--project", help="Exact project key")] = None,
    tool: Annotated[str | None, typer.Option("--tool", help="Exact MCP tool name")] = None,
    client: Annotated[str | None, typer.Option("--client", help="Exact client name")] = None,
) -> None:
    """Set filters shared by list/errors before the subcommand name."""

    ctx.obj = _ObservationScope(
        workspace=workspace,
        project=project,
        tool=tool,
        client=client,
    )


@observations_app.command("list")
def observations_list(
    since: Annotated[str | None, typer.Option("--since", help="1h/24h/7d/30d or ISO time")] = None,
    until: Annotated[str | None, typer.Option("--until", help="ISO time with timezone")] = None,
    failed: Annotated[bool, typer.Option("--failed", help="Show failures only")] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=500, callback=_limit_with_scope),
    ] = 50,
    json_output: Annotated[bool, typer.Option("--json", help="Emit structured JSON")] = False,
) -> None:
    """List bounded recent observations. Put shared filters before `list`."""

    scope = _scope_from_limit(limit)
    service = _service(scope.workspace)
    query = _query(scope, _TimeOptions(since, until), failed=True if failed else None, limit=limit)
    report = service.calls(query)
    if json_output:
        _echo_json(report)
    else:
        _write_calls(report)


@observations_app.command("errors")
def observations_errors(
    since: Annotated[str | None, typer.Option("--since", help="1h/24h/7d/30d or ISO time")] = None,
    until: Annotated[str | None, typer.Option("--until", help="ISO time with timezone")] = None,
    error_code: Annotated[str | None, typer.Option("--error-code", help="Exact error code")] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=500, callback=_limit_with_scope),
    ] = 50,
    json_output: Annotated[bool, typer.Option("--json", help="Emit structured JSON")] = False,
) -> None:
    """Show expected failures, internal fingerprints, and telemetry failures."""

    scope = _scope_from_limit(limit)
    service = _service(scope.workspace)
    report = service.errors(
        _query(
            scope,
            _TimeOptions(since, until),
            failed=True,
            limit=limit,
            error_code=error_code,
        )
    )
    if json_output:
        _echo_json(report)
    else:
        _write_errors(report)


@observations_app.command("show")
def observations_show(
    ctx: typer.Context,
    correlation_id: Annotated[str, typer.Argument(help="Opaque fm_corr_* or historical ID")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit structured JSON")] = False,
) -> None:
    """Join one exact correlation ID to its safe runtime diagnostic."""

    scope = _scope_from_context(ctx)
    service = _service(scope.workspace)
    report = service.observation(correlation_id)
    if json_output:
        _echo_json(report)
    else:
        _write_detail(report)


def doctor_command(
    workspace: Annotated[Path | None, WORKSPACE_OPTION] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit structured JSON")] = False,
) -> None:
    """Summarize scoped availability, activity, errors, performance, and runtime state."""

    service = _service(workspace)
    report = service.doctor()
    if json_output:
        _echo_json(report)
    else:
        _write_doctor(report)


def dashboard_command(
    workspace: Annotated[Path | None, WORKSPACE_OPTION] = None,
    port: Annotated[int, typer.Option("--port", min=1, max=65_535)] = 8765,
    open_browser: Annotated[bool, typer.Option("--open", help="Open the default browser")] = False,
) -> None:
    """Run the read-only operator console on 127.0.0.1 only."""

    from ferumind.dashboard import server

    server.serve_dashboard(workspace_root(workspace), port=port, open_browser=open_browser)
