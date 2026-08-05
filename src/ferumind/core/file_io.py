"""Durable, atomic file replacement primitives for core mutations."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(target: Path, content: str) -> None:
    """Replace *target* atomically with UTF-8 text and fsync the payload."""
    _atomic_write(target, content.encode("utf-8"))


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """Replace *target* atomically with bytes and fsync the payload."""
    _atomic_write(target, data)


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    fd, temporary_name = tempfile.mkstemp(dir=target.parent, prefix=".ferumind_tmp_")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
