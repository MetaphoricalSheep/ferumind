"""Privacy and durability guarantees for the private runtime event stream."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from ferumind.core.paths import WorkspaceRoot
from ferumind.core.runtime_events import (
    MAX_RUNTIME_EVENT_BYTES,
    ProcessStartedEvent,
    append_runtime_event,
    internal_error_event,
    observation_write_failed_event,
    read_runtime_events,
    runtime_log_path,
)

_CANARY = "signed-url-secret-canary"


class _MessageTrapError(RuntimeError):
    def __str__(self) -> str:
        raise AssertionError("runtime diagnostics accessed exception text")

    def __repr__(self) -> str:
        raise AssertionError("runtime diagnostics accessed exception repr")


def _caught_error() -> RuntimeError:
    try:
        raise RuntimeError(f"https://files.example.test/download?sig={_CANARY}")
    except RuntimeError as exc:
        return exc


def test_runtime_log_is_private_and_round_trips_typed_events(
    workspace: WorkspaceRoot,
) -> None:
    append_runtime_event(workspace, ProcessStartedEvent(package_version="0.1.0"))

    path = runtime_log_path(workspace)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    batch = read_runtime_events(workspace)
    assert batch.log_available is True
    assert [event.event for event in batch.events] == ["process_started"]


def test_internal_error_records_safe_frames_but_never_exception_text(
    workspace: WorkspaceRoot,
) -> None:
    event = internal_error_event(_caught_error(), "fm_corr_safe")
    append_runtime_event(workspace, event)

    raw = runtime_log_path(workspace).read_text(encoding="utf-8")
    assert _CANARY not in raw
    assert "https://files.example.test" not in raw
    assert event.exception_type.endswith("RuntimeError")
    assert event.stack_fingerprint.startswith("fm_stack_")
    assert event.frames
    assert all(not Path(frame.source_path).is_absolute() for frame in event.frames)
    assert any(frame.function == "_caught_error" for frame in event.frames)


def test_stack_fingerprint_is_stable_for_the_same_safe_stack_shape() -> None:
    events = [internal_error_event(_caught_error(), f"fm_corr_{index}") for index in range(2)]
    assert events[0].stack_fingerprint == events[1].stack_fingerprint


def test_internal_error_never_calls_exception_string_or_repr(
    workspace: WorkspaceRoot,
) -> None:
    event = internal_error_event(_MessageTrapError(_CANARY), "fm_corr_trap")
    append_runtime_event(workspace, event)
    assert event.exception_type.endswith("_MessageTrapError")
    assert _CANARY not in runtime_log_path(workspace).read_text(encoding="utf-8")


def test_observation_failure_retains_correlation_tool_and_safe_type(
    workspace: WorkspaceRoot,
) -> None:
    event = observation_write_failed_event(
        RuntimeError(_CANARY),
        correlation_id="fm_corr_missing_row",
        tool_name="get_context",
    )
    append_runtime_event(workspace, event)

    raw = runtime_log_path(workspace).read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert _CANARY not in raw
    assert payload["correlation_id"] == "fm_corr_missing_row"
    assert payload["tool_name"] == "get_context"
    assert payload["exception_type"].endswith("RuntimeError")
    assert payload["server_boot_id"]
    assert payload["process_id"] > 0


def test_reader_skips_malformed_incomplete_and_oversized_lines(
    workspace: WorkspaceRoot,
) -> None:
    append_runtime_event(workspace, ProcessStartedEvent(package_version="0.1.0"))
    path = runtime_log_path(workspace)
    with path.open("ab") as handle:
        handle.write(b'{"event":"process_started"\n')
        handle.write(
            b'{"event":"process_started","timestamp":"2026-01-01T00:00:00",'
            b'"package_version":"0.1.0"}\n'
        )
        handle.write(b"x" * (MAX_RUNTIME_EVENT_BYTES + 10) + b"\n")
        handle.write(
            ProcessStartedEvent(package_version="0.2.0").model_dump_json().encode() + b"\n"
        )
        handle.write(ProcessStartedEvent(package_version="0.3.0").model_dump_json().encode())

    batch = read_runtime_events(workspace)
    assert [
        event.package_version for event in batch.events if isinstance(event, ProcessStartedEvent)
    ] == [
        "0.2.0",
        "0.1.0",
    ]
    assert batch.malformed_lines == 3
    assert batch.oversized_lines == 1


@pytest.mark.parametrize("missing_field", ["timestamp", "server_boot_id", "process_id"])
def test_reader_rejects_events_missing_persisted_provenance(
    workspace: WorkspaceRoot,
    missing_field: str,
) -> None:
    path = runtime_log_path(workspace)
    path.parent.mkdir(mode=0o700, parents=True)
    incomplete = ProcessStartedEvent(package_version="incomplete").model_dump(mode="json")
    del incomplete[missing_field]
    valid = ProcessStartedEvent(package_version="valid")
    with path.open("wb") as handle:
        handle.write(json.dumps(incomplete).encode() + b"\n")
        handle.write(valid.model_dump_json().encode() + b"\n")

    batch = read_runtime_events(workspace)

    assert batch.malformed_lines == 1
    assert [
        event.package_version for event in batch.events if isinstance(event, ProcessStartedEvent)
    ] == ["valid"]


def test_reader_reports_missing_log_without_creating_it(workspace: WorkspaceRoot) -> None:
    batch = read_runtime_events(workspace)
    assert batch.log_available is False
    assert batch.events == ()
    assert not runtime_log_path(workspace).exists()


def test_writer_refuses_a_symlink_log_target(
    workspace: WorkspaceRoot,
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside.jsonl"
    target.write_text("untouched", encoding="utf-8")
    path = runtime_log_path(workspace)
    path.parent.mkdir(mode=0o700, parents=True)
    path.symlink_to(target)

    with pytest.raises((OSError, ValueError)):
        append_runtime_event(workspace, ProcessStartedEvent(package_version="0.1.0"))
    assert target.read_text(encoding="utf-8") == "untouched"


def test_writer_refuses_a_symlink_logs_directory(
    workspace: WorkspaceRoot,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-logs"
    outside.mkdir()
    logs = Path(workspace) / ".ferumind" / "logs"
    logs.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match=r"[Dd]irectory|[Ss]ymbolic|[Ll]evels"):
        append_runtime_event(workspace, ProcessStartedEvent(package_version="0.1.0"))
    assert list(outside.iterdir()) == []
