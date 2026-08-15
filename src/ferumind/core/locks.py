"""File, project, and workspace locking for safe concurrent operations.

Uses file-based locking via fcntl.flock() on POSIX.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections.abc import Generator
from pathlib import Path


class LockError(Exception):
    """Raised when a lock cannot be acquired."""


@contextlib.contextmanager
def _acquire_file_lock(lock_file: Path, label: str, timeout: float) -> Generator[None]:
    """Acquire an exclusive advisory lock on *lock_file*.

    Ferumind targets Linux, so an unavailable advisory lock is a hard error:
    silently continuing would make every hash check and collision check
    racy while presenting the operation as protected.
    """
    # `.ferumind/locks` is Ferumind's own state, not the operator's, so its
    # mode is re-asserted rather than merely set on create. See ``file_io``.
    lock_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_file.parent.chmod(0o700)
    lock_path = str(lock_file)
    start = time.monotonic()

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - start > timeout:
                    msg = f"Could not acquire lock for {label} within {timeout}s"
                    raise LockError(msg) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def acquire_project_lock(
    project_dir: Path, project_key: str, timeout: float = 10.0
) -> Generator[None]:
    """Acquire a project-level lock. Blocks until acquired or timeout."""
    from ferumind.core.paths import contained_path

    if not project_dir.is_dir():
        raise LockError("Cannot lock a project whose directory is missing")
    lock_file = contained_path(project_dir, f".ferumind/locks/{project_key}.lock")
    with _acquire_file_lock(lock_file, f"project {project_key}", timeout):
        yield


@contextlib.contextmanager
def acquire_workspace_lock(workspace_root: Path, timeout: float = 10.0) -> Generator[None]:
    """Acquire a workspace-level lock for workspace/admin mutations.

    Used for operations such as project creation where no project lock can
    exist yet because the project itself is being created.
    """
    from ferumind.core.paths import contained_path

    lock_file = contained_path(workspace_root, ".ferumind/locks/workspace.lock")
    with _acquire_file_lock(lock_file, "workspace", timeout):
        yield
