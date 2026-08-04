"""Regression tests for real advisory locking."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from lattice.core.locks import LockError, acquire_project_lock


def test_second_holder_times_out_while_lock_is_held(tmp_path: Path) -> None:
    lock_file = tmp_path / ".lattice" / "locks" / "project.lock"
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with acquire_project_lock(tmp_path, "project", timeout=1.0):
            entered.set()
            release.wait(timeout=2.0)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert entered.wait(timeout=1.0)
    try:
        with (
            pytest.raises(LockError),
            acquire_project_lock(tmp_path, "project", timeout=0.05),
        ):
            pytest.fail("a second holder entered the critical section")
    finally:
        release.set()
        holder.join(timeout=2.0)

    assert not holder.is_alive()
    assert lock_file.stat().st_mode & 0o777 == 0o600
