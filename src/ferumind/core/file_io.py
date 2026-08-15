"""Durable, atomic file replacement primitives for core mutations.

**Ferumind sets permissions when it creates something, and not afterwards.**

The two rules that follow from that are easy to get backwards, so they are
written down once here rather than repeated at each call site.

*Directories Ferumind creates for the operator* — project folders, ``compacts``,
``projects``, and every parent of a document — are created at ``0700`` and then
left alone. An operator who deliberately widens one to share a workspace over a
group-readable mount keeps that choice; silently restoring ``0700`` on the next
save is an undeclared side effect on a directory the operator owns, which is
exactly what this module's guarantee of "no hidden side effects" forbids.

*Directories that hold Ferumind's own private state* — everything under
``.ferumind/`` and the database — are still forced to ``0700`` on every touch,
because ``SECURITY.md`` promises they stay private and no operator workflow
needs them widened. Those call sites keep an explicit ``chmod`` and say why.

The same reasoning covers file modes: a new file is created private, and an
existing file's mode survives being rewritten.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def ensure_private_directory(path: Path) -> None:
    """Create *path* and any missing parents at ``0700``, touching no existing one.

    Every missing ancestor is created explicitly rather than through
    ``mkdir(parents=True)``, which applies *mode* to the final directory only
    and leaves intermediates at the process umask. A private leaf under a
    world-listable parent is not private, and that gap is invisible until
    someone lists the parent.
    """
    missing: list[Path] = []
    current = path
    while not current.is_dir():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)


def atomic_write_text(target: Path, content: str) -> None:
    """Replace *target* atomically with UTF-8 text and fsync the payload."""
    _atomic_write(target, content.encode("utf-8"))


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """Replace *target* atomically with bytes and fsync the payload."""
    _atomic_write(target, data)


def _existing_file_mode(target: Path) -> int | None:
    """Return *target*'s mode when it is an existing regular file, else ``None``.

    ``lstat`` rather than ``stat``: a replace swaps the name itself, so the
    thing being replaced is the link and not its destination. Anything that is
    not a plain file — a symlink, a socket, a directory — reports ``None`` and
    is written as if new, so an odd inode can never talk this into applying a
    permissive mode.
    """
    try:
        status = target.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not stat.S_ISREG(status.st_mode):
        return None
    return stat.S_IMODE(status.st_mode)


def _atomic_write(target: Path, data: bytes) -> None:
    ensure_private_directory(target.parent)
    preserved_mode = _existing_file_mode(target)
    fd, temporary_name = tempfile.mkstemp(dir=target.parent, prefix=".ferumind_tmp_")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # A rename carries the temporary file's own mode onto the destination,
        # so without this an operator-chosen mode is reset to mkstemp's 0600 by
        # every save. New files keep 0600, which is the private default.
        if preserved_mode is not None:
            temporary.chmod(preserved_mode)
        temporary.replace(target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
