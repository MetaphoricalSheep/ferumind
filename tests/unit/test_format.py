"""Tests for the workspace format marker and gate (spec-versioning §1)."""

from __future__ import annotations

import os

import pytest

from lattice.core.errors import FormatUnsupportedError
from lattice.core.format import (
    SUPPORTED_FORMAT,
    FormatGate,
    meta_path,
    read_format,
    write_format_marker,
)
from lattice.core.paths import WorkspaceRoot


def test_bootstrap_workspace_carries_format_2(workspace: WorkspaceRoot) -> None:
    assert read_format(workspace) == SUPPORTED_FORMAT == 2


def test_write_format_marker_preserves_created(workspace: WorkspaceRoot) -> None:
    original = meta_path(workspace).read_text(encoding="utf-8")
    created_line = next(line for line in original.splitlines() if line.startswith("created:"))
    write_format_marker(workspace, 3)
    updated = meta_path(workspace).read_text(encoding="utf-8")
    assert "format: 3" in updated
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
    write_format_marker(workspace, 1)
    gate = FormatGate(workspace)
    gate.check_read()
    with pytest.raises(FormatUnsupportedError, match="lattice migrate"):
        gate.check_write()


def test_gate_missing_marker_treated_as_older(workspace: WorkspaceRoot) -> None:
    meta_path(workspace).unlink()
    gate = FormatGate(workspace)
    gate.check_read()
    with pytest.raises(FormatUnsupportedError):
        gate.check_write()


def test_gate_newer_format_refuses_everything(workspace: WorkspaceRoot) -> None:
    write_format_marker(workspace, SUPPORTED_FORMAT + 1)
    gate = FormatGate(workspace)
    with pytest.raises(FormatUnsupportedError, match="upgrade"):
        gate.check_read()
    with pytest.raises(FormatUnsupportedError, match="upgrade"):
        gate.check_write()


def test_gate_error_details_carry_versions(workspace: WorkspaceRoot) -> None:
    write_format_marker(workspace, 1)
    gate = FormatGate(workspace)
    with pytest.raises(FormatUnsupportedError) as excinfo:
        gate.check_write()
    assert excinfo.value.details == {"found_format": 1, "supported_format": SUPPORTED_FORMAT}


def test_gate_reacts_to_out_of_band_marker_edit(workspace: WorkspaceRoot) -> None:
    gate = FormatGate(workspace)
    gate.check_write()  # populate the cache
    write_format_marker(workspace, 1)
    # Force a visible mtime change even on coarse filesystem clocks.
    stat = meta_path(workspace).stat()
    os.utime(meta_path(workspace), ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    with pytest.raises(FormatUnsupportedError):
        gate.check_write()
