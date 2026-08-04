"""Workspace format version marker and gate (product/spec-versioning.md §1).

The workspace format lives in ``workspace/system/meta.yml`` (``format: 2``,
whole-workspace granularity). The server supports exactly one format:

- format == supported → reads and writes normal
- format < supported (or marker missing) → reads allowed, writes refused
  with ``FORMAT_UNSUPPORTED`` (run ``lattice migrate``)
- format > supported → everything refused with ``FORMAT_UNSUPPORTED``
  (upgrade the server)

The check is a cheap stat + parse, cached and re-read when meta.yml's mtime
changes — the marker is subject to out-of-band edits like everything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

import yaml

from lattice.core.errors import FormatUnsupportedError
from lattice.core.file_io import atomic_write_text
from lattice.core.paths import WorkspaceRoot, contained_path
from lattice.core.yaml_safe import safe_load_yaml

SUPPORTED_FORMAT: Final = 2

_META_HEADER: Final = "# Lattice workspace metadata. Managed by Lattice; do not edit by hand.\n"
MAX_FORMAT_MARKER_BYTES: Final = 16 * 1024


def meta_path(workspace: WorkspaceRoot) -> Path:
    return contained_path(workspace, "system/meta.yml")


def read_format(workspace: WorkspaceRoot) -> int | None:
    """Return the workspace format, or ``None`` when the marker is missing/unreadable.

    A missing marker means a pre-format workspace (treated as format 1).
    """
    path = meta_path(workspace)
    if not path.is_file():
        return None
    try:
        data = safe_load_yaml(
            path.read_text(encoding="utf-8"),
            max_bytes=MAX_FORMAT_MARKER_BYTES,
        )
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    raw = cast(dict[object, object], data).get("format")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def write_format_marker(workspace: WorkspaceRoot, format_version: int = SUPPORTED_FORMAT) -> Path:
    """Write (or rewrite) ``system/meta.yml``, preserving the created date."""
    path = meta_path(workspace)
    created = datetime.now(UTC).date().isoformat()
    if path.is_file():
        try:
            data = safe_load_yaml(
                path.read_text(encoding="utf-8"),
                max_bytes=MAX_FORMAT_MARKER_BYTES,
            )
        except (yaml.YAMLError, OSError):
            data = None
        if isinstance(data, dict):
            existing = cast(dict[object, object], data).get("created")
            if isinstance(existing, str) and existing:
                created = existing
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        f'{_META_HEADER}format: {format_version}\ncreated: "{created}"\n',
    )
    return path


@dataclass
class _CachedFormat:
    mtime_ns: int | None
    value: int | None


class FormatGate:
    """Per-process cached format check for a workspace."""

    def __init__(self, workspace: WorkspaceRoot, supported: int = SUPPORTED_FORMAT) -> None:
        self._workspace = workspace
        self._supported = supported
        self._cache: _CachedFormat | None = None

    @property
    def supported(self) -> int:
        return self._supported

    def current_format(self) -> int | None:
        """Return the workspace format, re-reading when meta.yml changed on disk."""
        path = meta_path(self._workspace)
        try:
            mtime_ns: int | None = path.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if self._cache is not None and self._cache.mtime_ns == mtime_ns:
            return self._cache.value
        value = read_format(self._workspace)
        self._cache = _CachedFormat(mtime_ns=mtime_ns, value=value)
        return value

    def check_read(self) -> None:
        """Gate a read: only a newer-than-supported workspace is refused."""
        found = self.current_format()
        if found is not None and found > self._supported:
            raise FormatUnsupportedError(
                self._newer_message(found),
                details={"found_format": found, "supported_format": self._supported},
            )

    def check_write(self) -> None:
        """Gate a write: any format mismatch (or missing marker) is refused."""
        found = self.current_format()
        if found == self._supported:
            return
        if found is not None and found > self._supported:
            message = self._newer_message(found)
        else:
            message = (
                f"Workspace format {found if found is not None else 1} is older than "
                f"the supported format {self._supported}; reads remain available, "
                "writes are refused. Remedy: run `lattice migrate`."
            )
        raise FormatUnsupportedError(
            message,
            details={"found_format": found, "supported_format": self._supported},
        )

    def _newer_message(self, found: int) -> str:
        return (
            f"Workspace format {found} is newer than the supported format "
            f"{self._supported}; refusing all access. Remedy: upgrade the Lattice server."
        )
