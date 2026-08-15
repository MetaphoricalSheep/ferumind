"""Tests for the workspace format marker and gate (spec-versioning §1)."""

from __future__ import annotations

import os

import pytest

from ferumind.core.errors import FormatUnsupportedError
from ferumind.core.format import (
    SUPPORTED_FORMAT,
    FormatGate,
    meta_path,
    read_format,
    write_format_marker,
)
from ferumind.core.paths import WorkspaceRoot


def test_bootstrap_workspace_carries_current_format(workspace: WorkspaceRoot) -> None:
    assert read_format(workspace) == SUPPORTED_FORMAT == 1


def test_write_format_marker_preserves_created(workspace: WorkspaceRoot) -> None:
    original = meta_path(workspace).read_text(encoding="utf-8")
    created_line = next(line for line in original.splitlines() if line.startswith("created:"))
    replacement_format = SUPPORTED_FORMAT + 1
    write_format_marker(workspace, replacement_format)
    updated = meta_path(workspace).read_text(encoding="utf-8")
    assert f"format: {replacement_format}" in updated
    assert created_line in updated


def test_read_format_missing_or_garbled(workspace: WorkspaceRoot) -> None:
    meta_path(workspace).write_text("format: not-a-number\n", encoding="utf-8")
    assert read_format(workspace) is None
    meta_path(workspace).unlink()
    assert read_format(workspace) is None


def test_gate_matching_format_allows_everything(workspace: WorkspaceRoot) -> None:
    gate = FormatGate(workspace)
    gate.check_read()
    gate.check_write()


def test_gate_older_format_allows_reads_refuses_writes(workspace: WorkspaceRoot) -> None:
    """Format 1 is the floor, so the older side is expressed as a newer build.

    ``supported`` is a constructor parameter precisely so this relation can be
    proved without a format below the floor. It is the state a user reaches by
    upgrading Ferumind before running ``ferumind migrate``.
    """
    gate = FormatGate(workspace, supported=SUPPORTED_FORMAT + 1)
    gate.check_read()
    with pytest.raises(FormatUnsupportedError, match="ferumind migrate"):
        gate.check_write()


def test_gate_missing_marker_refuses_writes_without_inventing_a_format(
    workspace: WorkspaceRoot,
) -> None:
    """Reads stay open, writes are refused, and no format number is fabricated."""
    meta_path(workspace).unlink()
    gate = FormatGate(workspace)
    gate.check_read()
    with pytest.raises(FormatUnsupportedError) as excinfo:
        gate.check_write()
    message = str(excinfo.value)
    assert "not an initialized Ferumind workspace" in message
    assert "bootstrap_workspace.py" in message
    assert "format 1" not in message
    assert "ferumind migrate" not in message


def test_gate_newer_format_refuses_everything(workspace: WorkspaceRoot) -> None:
    write_format_marker(workspace, SUPPORTED_FORMAT + 1)
    gate = FormatGate(workspace)
    with pytest.raises(FormatUnsupportedError, match="upgrade"):
        gate.check_read()
    with pytest.raises(FormatUnsupportedError, match="upgrade"):
        gate.check_write()


def test_gate_error_details_carry_versions(workspace: WorkspaceRoot) -> None:
    gate = FormatGate(workspace, supported=SUPPORTED_FORMAT + 1)
    with pytest.raises(FormatUnsupportedError) as excinfo:
        gate.check_write()
    assert excinfo.value.details == {
        "found_format": SUPPORTED_FORMAT,
        "supported_format": SUPPORTED_FORMAT + 1,
    }


def test_gate_reacts_to_out_of_band_marker_edit(workspace: WorkspaceRoot) -> None:
    gate = FormatGate(workspace)
    gate.check_write()  # populate the cache
    write_format_marker(workspace, SUPPORTED_FORMAT + 1)
    # Force a visible mtime change even on coarse filesystem clocks.
    stat = meta_path(workspace).stat()
    os.utime(meta_path(workspace), ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    with pytest.raises(FormatUnsupportedError):
        gate.check_write()
