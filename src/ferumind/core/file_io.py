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

import contextlib
import errno
import os
import secrets
import stat
from pathlib import Path

from ferumind.core.paths import PathSafetyError


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


def _open_no_follow(target: Path) -> int:
    """Open *target* read-only, refusing a symlink at the final component.

    ``contained_path`` refuses symlinks when it validates, but it hands back a
    ``Path`` that somebody opens later, and the two are not the same instant.
    ``O_NOFOLLOW`` makes a symlink swapped into that window an error rather
    than a silent redirect (S-06).

    ``ELOOP`` is reported as :class:`PathSafetyError` so a swapped component
    reaches the caller the same way a statically detected one does — the MCP
    layer maps both to ``WORKSPACE_MISMATCH``. Every other ``OSError``,
    ``FileNotFoundError`` included, is left alone for callers that already
    distinguish them.
    """
    try:
        return os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            msg = f"Symlinks are not allowed below root: {target}"
            raise PathSafetyError(msg) from exc
        raise


def read_regular_file_bytes(target: Path) -> bytes:
    """Return *target*'s bytes, proving on the descriptor that it is a real file.

    The regular-file check runs against ``fstat`` of the *opened* descriptor
    rather than a separate ``stat`` of the path, so the file that was checked
    is necessarily the file that is read. A path-based check answers a
    question about a name; this answers it about the object.
    """
    fd = _open_no_follow(target)
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            msg = f"Not a regular file: {target}"
            raise PathSafetyError(msg)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def atomic_write_text(target: Path, content: str) -> None:
    """Replace *target* atomically with UTF-8 text and fsync the payload."""
    _atomic_write(target, content.encode("utf-8"))


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """Replace *target* atomically with bytes and fsync the payload."""
    _atomic_write(target, data)


def _existing_file_mode(name: str, directory_fd: int) -> int | None:
    """Return *name*'s mode when it is an existing regular file, else ``None``.

    ``lstat`` rather than ``stat``: a replace swaps the name itself, so the
    thing being replaced is the link and not its destination. Anything that is
    not a plain file — a symlink, a socket, a directory — reports ``None`` and
    is written as if new, so an odd inode can never talk this into applying a
    permissive mode.
    """
    try:
        status = os.lstat(name, dir_fd=directory_fd)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not stat.S_ISREG(status.st_mode):
        return None
    return stat.S_IMODE(status.st_mode)


def _atomic_write(target: Path, data: bytes) -> None:
    ensure_private_directory(target.parent)
    # The parent is opened once and every step below is relative to that
    # descriptor, so the directory this call validated is necessarily the
    # directory it writes into — renaming a component afterwards moves the
    # tree, not the descriptor (S-06).
    #
    # O_NOFOLLOW is deliberately absent here, and only here. ``contained_path``
    # allows the configured root itself to be a symlink, because that root is
    # an operator-selected trust boundary, and for a top-level document the
    # parent *is* that root — refusing it would break a supported layout.
    # Components below the root are symlink-free by validation, and the final
    # component is covered by O_EXCL and the descriptor-relative rename.
    directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        preserved_mode = _existing_file_mode(target.name, directory_fd)
        # A private random name rather than mkstemp, which resolves its ``dir``
        # argument as a path and would reintroduce the race this descriptor
        # closes. O_EXCL makes a collision an error, never a silent reuse.
        temporary_name = f".ferumind_tmp_{secrets.token_hex(8)}"
        fd = os.open(
            temporary_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                # A rename carries the temporary file's own mode onto the
                # destination, so without this an operator-chosen mode is reset
                # to 0600 by every save. New files keep 0600, the private
                # default. Applied through the descriptor, never the name.
                if preserved_mode is not None:
                    os.fchmod(handle.fileno(), preserved_mode)
                os.fsync(handle.fileno())
            os.replace(
                temporary_name,
                target.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        except BaseException:
            # A successful rename has already consumed the temporary name, so
            # there may be nothing left to remove.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)
            raise
    finally:
        os.close(directory_fd)
