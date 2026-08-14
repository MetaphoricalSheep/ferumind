"""Private, metadata-only runtime diagnostics.

Normal MCP calls belong in :mod:`ferumind.core.observations`.  This module is
the much smaller exceptional/lifecycle stream used to explain missing
observations, internal failures, and process boundaries without persisting
request data or exception messages.  Public boundaries use discriminated,
strict models; arbitrary dictionaries never cross into the writer.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import stat
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, BinaryIO, Literal

from pydantic import Field, TypeAdapter, field_validator

from ferumind.core.observations import PROCESS_ID, SERVER_BOOT_ID
from ferumind.core.paths import PathSafetyError, WorkspaceRoot, contained_path
from ferumind.core.types import StrictModel

logger = logging.getLogger(__name__)

RUNTIME_LOG_RELATIVE_PATH = ".ferumind/logs/ferumind.jsonl"
_RUNTIME_LOG_FILENAME = "ferumind.jsonl"
MAX_RUNTIME_EVENT_BYTES = 64 * 1024
MAX_SAFE_FRAMES = 32
DEFAULT_RUNTIME_EVENT_LIMIT = 200
MAX_RUNTIME_EVENT_LIMIT = 2_000

type TelemetryFailureStage = Literal[
    "result_interpretation",
    "metric_extraction",
    "result_size",
    "client_identity",
    "observation_persistence",
]
type TransportCloseReason = Literal[
    "eof",
    "request_too_large",
    "server_stopping",
    "unknown",
]
type _RuntimeLineStatus = Literal["event", "eof", "malformed", "oversized"]


def _safe_error_log(message: str, *args: object) -> None:
    """Log diagnostics best-effort; a broken handler is telemetry failure too."""

    try:
        logger.error(message, *args)
    except Exception:  # Runtime diagnostics are strictly non-interfering.
        return


def _now() -> datetime:
    return datetime.now(UTC)


class SafeStackFrame(StrictModel):
    """A traceback location with no source text, locals, or absolute path."""

    module: str = Field(max_length=256)
    source_path: str = Field(max_length=512)
    function: str = Field(max_length=256)
    line: int = Field(ge=0)


class RuntimeEventBase(StrictModel):
    """Fields shared by every durable runtime event."""

    timestamp: datetime = Field(default_factory=_now)
    server_boot_id: str = Field(default=SERVER_BOOT_ID, max_length=256)
    process_id: int = Field(default=PROCESS_ID, ge=0)

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Runtime event timestamps must include a timezone")
        return value


class ProcessStartedEvent(RuntimeEventBase):
    event: Literal["process_started"] = "process_started"
    transport: str = Field(default="stdio", max_length=64)
    package_version: str = Field(max_length=128)


class ClientInitializedEvent(RuntimeEventBase):
    event: Literal["client_initialized"] = "client_initialized"
    client_name: str | None = Field(default=None, max_length=256)
    client_version: str | None = Field(default=None, max_length=128)
    protocol_version: str | None = Field(default=None, max_length=128)


class TransportClosedEvent(RuntimeEventBase):
    event: Literal["transport_closed"] = "transport_closed"
    reason: TransportCloseReason = "unknown"


class ProcessStoppingEvent(RuntimeEventBase):
    event: Literal["process_stopping"] = "process_stopping"
    reason: Literal["normal", "keyboard_interrupt", "server_exit"] = "normal"


class InternalErrorEvent(RuntimeEventBase):
    event: Literal["internal_error"] = "internal_error"
    correlation_id: str = Field(min_length=1, max_length=256)
    exception_type: str = Field(min_length=1, max_length=256)
    stack_fingerprint: str = Field(min_length=1, max_length=128)
    frames: tuple[SafeStackFrame, ...] = Field(max_length=MAX_SAFE_FRAMES)


class ObservationWriteFailedEvent(RuntimeEventBase):
    event: Literal["observation_write_failed"] = "observation_write_failed"
    correlation_id: str = Field(min_length=1, max_length=256)
    tool_name: str = Field(min_length=1, max_length=256)
    exception_type: str = Field(min_length=1, max_length=256)
    stage: TelemetryFailureStage = "observation_persistence"


class MalformedRequestEvent(RuntimeEventBase):
    event: Literal["malformed_request"] = "malformed_request"
    exception_type: str = Field(min_length=1, max_length=256)


class RequestTooLargeEvent(RuntimeEventBase):
    event: Literal["request_too_large"] = "request_too_large"
    limit_bytes: int = Field(ge=1)
    received_at_least_bytes: int = Field(ge=1)


type RuntimeEvent = Annotated[
    ProcessStartedEvent
    | ClientInitializedEvent
    | TransportClosedEvent
    | ProcessStoppingEvent
    | InternalErrorEvent
    | ObservationWriteFailedEvent
    | MalformedRequestEvent
    | RequestTooLargeEvent,
    Field(discriminator="event"),
]
type RuntimeEventFilter = Literal["all", "lifecycle"]

_RUNTIME_EVENT_ADAPTER: TypeAdapter[RuntimeEvent] = TypeAdapter(RuntimeEvent)
_RUNTIME_LINE_OBJECT_ADAPTER: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])
_REQUIRED_PERSISTED_FIELDS = frozenset({"timestamp", "server_boot_id", "process_id"})
_LIFECYCLE_EVENT_KINDS = frozenset(
    {"process_started", "client_initialized", "transport_closed", "process_stopping"}
)


class RuntimeEventBatch(StrictModel):
    """A bounded, newest-first read from the private runtime log."""

    log_available: bool
    events: tuple[RuntimeEvent, ...]
    malformed_lines: int = Field(default=0, ge=0)
    oversized_lines: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class RuntimeEventQuery:
    """Typed bounds for one private runtime-log read."""

    limit: int = DEFAULT_RUNTIME_EVENT_LIMIT
    correlation_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    event_filter: RuntimeEventFilter = "all"


def runtime_log_path(workspace: WorkspaceRoot) -> Path:
    """Return the canonical private runtime log path under *workspace*."""

    return contained_path(workspace, RUNTIME_LOG_RELATIVE_PATH)


def _directory_open_flags() -> int:
    flags = os.O_CLOEXEC | os.O_DIRECTORY | os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_runtime_logs_directory(workspace: WorkspaceRoot, *, create: bool) -> int | None:
    """Open the private logs directory without following workspace children."""

    try:
        root_fd = os.open(Path(workspace).resolve(), _directory_open_flags())
    except FileNotFoundError:
        if create:
            raise
        return None
    try:
        try:
            ferumind_fd = os.open(".ferumind", _directory_open_flags(), dir_fd=root_fd)
        except FileNotFoundError:
            if create:
                raise
            return None
    finally:
        os.close(root_fd)

    try:
        if create:
            with suppress(FileExistsError):
                os.mkdir("logs", mode=0o700, dir_fd=ferumind_fd)
        try:
            logs_fd = os.open("logs", _directory_open_flags(), dir_fd=ferumind_fd)
        except FileNotFoundError:
            return None
    finally:
        os.close(ferumind_fd)
    if create:
        try:
            os.fchmod(logs_fd, 0o700)
        except Exception:
            os.close(logs_fd)
            raise
    return logs_fd


def _validate_runtime_log_descriptor(fd: int) -> None:
    descriptor = os.fstat(fd)
    if not stat.S_ISREG(descriptor.st_mode) or descriptor.st_nlink != 1:
        raise PathSafetyError("The private runtime log must be a single-link regular file")


def _open_runtime_log(workspace: WorkspaceRoot, *, create: bool) -> int | None:
    logs_fd = _open_runtime_logs_directory(workspace, create=create)
    if logs_fd is None:
        return None
    flags = os.O_CLOEXEC | os.O_NONBLOCK | (os.O_APPEND | os.O_WRONLY if create else os.O_RDONLY)
    if create:
        flags |= os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        try:
            fd = os.open(_RUNTIME_LOG_FILENAME, flags, 0o600, dir_fd=logs_fd)
        except FileNotFoundError:
            return None
    finally:
        os.close(logs_fd)
    try:
        _validate_runtime_log_descriptor(fd)
        if create:
            os.fchmod(fd, 0o600)
    except Exception:
        os.close(fd)
        raise
    return fd


def append_runtime_event(workspace: WorkspaceRoot, event: RuntimeEvent) -> None:
    """Append one typed event with private permissions and a process-safe lock.

    This low-level writer deliberately raises.  User-facing call paths use
    :func:`try_append_runtime_event`, whose broad catch is the structural
    guarantee that a diagnostic failure cannot replace the result being
    diagnosed.
    """

    payload = event.model_dump_json(exclude_none=True).encode("utf-8") + b"\n"
    if len(payload) > MAX_RUNTIME_EVENT_BYTES:
        raise ValueError("Runtime event exceeds the private log record limit")

    fd = _open_runtime_log(workspace, create=True)
    if fd is None:  # Creation either returns a descriptor or raises.
        raise RuntimeError("Private runtime log creation did not produce a file")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def try_append_runtime_event(workspace: WorkspaceRoot, event: RuntimeEvent) -> bool:
    """Best-effort event persistence that can never interrupt caller work."""

    try:
        append_runtime_event(workspace, event)
    except Exception as exc:  # Runtime diagnostics are strictly non-interfering.
        _safe_error_log(
            "Failed to persist a private runtime event (event=%s, type=%s)",
            event.event,
            type(exc).__name__,
        )
        return False
    return True


def try_record_internal_error(
    workspace: WorkspaceRoot,
    exc: BaseException,
    correlation_id: str,
) -> bool:
    """Build and persist an internal-error event without affecting its caller.

    Event construction is inside the containment boundary as deliberately as
    file I/O. Traceback inspection and validation are telemetry too; neither
    may replace the generic MCP error that this record is meant to explain.
    """

    try:
        event = internal_error_event(exc, correlation_id)
        append_runtime_event(workspace, event)
    except Exception as diagnostic_exc:  # Diagnostics are strictly non-interfering.
        _safe_error_log(
            "Failed to persist a private internal-error event (type=%s)",
            type(diagnostic_exc).__name__,
        )
        return False
    return True


def _safe_text(value: object, *, fallback: str, max_length: int) -> str:
    if not isinstance(value, str) or not value:
        return fallback
    cleaned = "".join(character for character in value if character.isprintable())
    return (cleaned or fallback)[:max_length]


def exception_type_name(exc: BaseException) -> str:
    """Return a stable type label without touching ``str``/``repr`` of *exc*."""

    cls = type(exc)
    module = _safe_text(cls.__module__, fallback="builtins", max_length=128)
    qualname = _safe_text(cls.__qualname__, fallback=cls.__name__, max_length=128)
    return f"{module}.{qualname}"[:256]


def _logical_source_path(filename: str, module: str) -> str:
    """Turn a traceback filename into a non-absolute package/repository path."""

    if filename.startswith("<") and filename.endswith(">"):
        # ``compile`` accepts an arbitrary caller-supplied filename. Treat all
        # pseudo paths alike rather than persisting that string as metadata.
        return "<dynamic>"
    candidate = Path(filename)
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        resolved = candidate

    package_root = Path(__file__).resolve().parents[1]
    try:
        return resolved.relative_to(package_root.parent).as_posix()[:512]
    except ValueError:
        pass

    logical = module.replace(".", "/") or "external"
    suffix = Path(filename).suffix if Path(filename).suffix in {".py", ".pyi"} else ""
    return f"{logical}{suffix}"[:512]


def safe_stack_frames(exc: BaseException) -> tuple[SafeStackFrame, ...]:
    """Extract only code locations; never exception text, arguments, or locals."""

    frames: deque[SafeStackFrame] = deque(maxlen=MAX_SAFE_FRAMES)
    traceback = exc.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module = _safe_text(frame.f_globals.get("__name__"), fallback="unknown", max_length=256)
        function = _safe_text(frame.f_code.co_name, fallback="unknown", max_length=256)
        frames.append(
            SafeStackFrame(
                module=module,
                source_path=_logical_source_path(frame.f_code.co_filename, module),
                function=function,
                line=max(0, traceback.tb_lineno),
            )
        )
        traceback = traceback.tb_next
    return tuple(frames)


def stack_fingerprint(exception_type: str, frames: tuple[SafeStackFrame, ...]) -> str:
    """Hash the safe stack shape so repeated failures group deterministically."""

    shape = {
        "exception_type": exception_type,
        "frames": [frame.model_dump(mode="json") for frame in frames],
    }
    encoded = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"fm_stack_{hashlib.sha256(encoded).hexdigest()[:32]}"


def internal_error_event(exc: BaseException, correlation_id: str) -> InternalErrorEvent:
    """Build the safe durable diagnostic paired with an ``INTERNAL_ERROR``."""

    frames = safe_stack_frames(exc)
    exc_type = exception_type_name(exc)
    return InternalErrorEvent(
        correlation_id=_safe_text(correlation_id, fallback="unknown", max_length=256),
        exception_type=exc_type,
        stack_fingerprint=stack_fingerprint(exc_type, frames),
        frames=frames,
    )


def observation_write_failed_event(
    exc: BaseException,
    *,
    correlation_id: str,
    tool_name: str,
    stage: TelemetryFailureStage = "observation_persistence",
) -> ObservationWriteFailedEvent:
    """Build a privacy-safe fallback when post-call telemetry fails."""

    return ObservationWriteFailedEvent(
        correlation_id=_safe_text(correlation_id, fallback="unknown", max_length=256),
        tool_name=_safe_text(tool_name, fallback="<unknown>", max_length=256),
        exception_type=exception_type_name(exc),
        stage=stage,
    )


def _drain_oversized_line(handle: BinaryIO) -> None:
    """Consume the rest of one over-limit binary line without retaining it."""

    while True:
        chunk = handle.readline(MAX_RUNTIME_EVENT_BYTES + 1)
        if not chunk or chunk.endswith(b"\n"):
            return


def _read_runtime_line(
    handle: BinaryIO,
) -> tuple[_RuntimeLineStatus, RuntimeEvent | None]:
    raw = handle.readline(MAX_RUNTIME_EVENT_BYTES + 1)
    if not raw:
        return "eof", None
    if len(raw) > MAX_RUNTIME_EVENT_BYTES:
        if not raw.endswith(b"\n"):
            _drain_oversized_line(handle)
        return "oversized", None
    if not raw.endswith(b"\n"):
        return "malformed", None
    try:
        payload = _RUNTIME_LINE_OBJECT_ADAPTER.validate_json(raw)
        if not _REQUIRED_PERSISTED_FIELDS.issubset(payload):
            return "malformed", None
        return "event", _RUNTIME_EVENT_ADAPTER.validate_python(payload)
    except (UnicodeDecodeError, ValueError):
        return "malformed", None


def _event_matches(event: RuntimeEvent, filters: RuntimeEventQuery) -> bool:
    if filters.event_filter == "lifecycle" and event.event not in _LIFECYCLE_EVENT_KINDS:
        return False
    event_correlation = getattr(event, "correlation_id", None)
    if filters.correlation_id is not None and event_correlation != filters.correlation_id:
        return False
    if filters.start is not None and event.timestamp < filters.start:
        return False
    return filters.end is None or event.timestamp <= filters.end


def read_runtime_events(
    workspace: WorkspaceRoot,
    query: RuntimeEventQuery | None = None,
) -> RuntimeEventBatch:
    """Read valid typed events, optionally retaining lifecycle records only."""

    filters = query or RuntimeEventQuery()
    bounded_limit = max(1, min(filters.limit, MAX_RUNTIME_EVENT_LIMIT))
    fd = _open_runtime_log(workspace, create=False)
    if fd is None:
        return RuntimeEventBatch(log_available=False, events=())

    recent: deque[RuntimeEvent] = deque(maxlen=bounded_limit)
    malformed = 0
    oversized = 0
    with os.fdopen(fd, "rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            while True:
                status, event = _read_runtime_line(handle)
                if status == "eof":
                    break
                if status == "oversized":
                    oversized += 1
                    continue
                if status == "malformed":
                    malformed += 1
                    continue
                if event is not None and _event_matches(event, filters):
                    recent.append(event)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return RuntimeEventBatch(
        log_available=True,
        events=tuple(reversed(recent)),
        malformed_lines=malformed,
        oversized_lines=oversized,
    )
