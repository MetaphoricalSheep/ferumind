"""Snapshot primitives for point-in-time file versioning.

This module provides low-level snapshot helpers only. Restore mutations go
through the central write service (writes.py) so restore itself is
snapshot-protected, logged, scoped, and indexed. Snapshot directories live
under ``projects/<key>/.ferumind/snapshots/`` (per-file snapshots) and
``workspace/.ferumind/global-snapshots/`` (workspace-scoped snapshots such as
project creation and migration).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict
from pydantic_core import ValidationError as PydanticValidationError

from ferumind.core.file_io import atomic_write_bytes, atomic_write_text
from ferumind.core.paths import PathSafetyError, contained_path
from ferumind.core.types import DbConnection, JsonObject

MAX_SNAPSHOT_METADATA_BYTES = 64 * 1024


class SnapshotInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_key: str
    target_path: str | None = None
    snapshot_dir: str
    reason: str
    created_at: str


class SnapshotMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_key: str
    operation_type: str
    target_path: str | None = None
    created_at: str
    reason: str
    before_sha256: str | None = None
    before_size_bytes: int | None = None
    after_sha256: str | None = None
    after_size_bytes: int | None = None


class GlobalSnapshotMetadata(BaseModel):
    """Metadata for a workspace-scoped (global) snapshot."""

    model_config = ConfigDict(extra="forbid")

    id: str
    scope: str
    operation_type: str
    target_project_key: str | None = None
    created_at: str
    reason: str


def new_snapshot_id() -> str:
    return str(uuid.uuid4())


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


@contextmanager
def _snapshot_construction(snapshot_dir: Path) -> Generator[None]:
    """Publish no partial snapshot when construction raises.

    Snapshot ids are unique, so an existing destination is a hard collision,
    never a directory to reuse or clean up.
    """
    snapshot_dir.mkdir(mode=0o700, exist_ok=False)
    try:
        yield
    except BaseException as exc:
        try:
            shutil.rmtree(snapshot_dir)
        except OSError as cleanup_exc:
            exc.add_note(
                "Ferumind also failed to remove the incomplete snapshot "
                f"({type(cleanup_exc).__name__})"
            )
        raise


def create_snapshot(
    project_dir: Path,
    *,
    project_key: str,
    target_path: str | None,
    before_content: str | None,
    after_content: str | None,
    reason: str,
    snapshot_id: str,
) -> Path:
    """Create a snapshot directory with before/after/diff/metadata.

    Returns the path to the created snapshot directory.
    """
    now = datetime.now(UTC)
    ts = now.strftime("%Y%m%dT%H%M%S")
    snapshots_base = contained_path(project_dir, ".ferumind/snapshots")
    _private_directory(snapshots_base)
    snapshot_dir = contained_path(snapshots_base, f"{ts}-{snapshot_id}")
    with _snapshot_construction(snapshot_dir):
        before_dir = snapshot_dir / "before"
        after_dir = snapshot_dir / "after"

        if before_content is not None:
            _private_directory(before_dir)
            _write_snapshot_file(before_dir, target_path or "", before_content)

        if after_content is not None:
            _private_directory(after_dir)
            _write_snapshot_file(after_dir, target_path or "", after_content)

        diff = _generate_diff(before_content, after_content, target_path)
        atomic_write_text(contained_path(snapshot_dir, "diff.patch"), diff)

        before_bytes = before_content.encode("utf-8") if before_content is not None else None
        after_bytes = after_content.encode("utf-8") if after_content is not None else None
        metadata = SnapshotMetadata(
            id=snapshot_id,
            project_key=project_key,
            operation_type=reason,
            target_path=target_path,
            created_at=now.isoformat(),
            reason=reason,
            before_sha256=hashlib.sha256(before_bytes).hexdigest()
            if before_bytes is not None
            else None,
            before_size_bytes=len(before_bytes) if before_bytes is not None else None,
            after_sha256=hashlib.sha256(after_bytes).hexdigest()
            if after_bytes is not None
            else None,
            after_size_bytes=len(after_bytes) if after_bytes is not None else None,
        )
        atomic_write_text(
            contained_path(snapshot_dir, "metadata.json"),
            metadata.model_dump_json(indent=2),
        )

    return snapshot_dir


def create_global_snapshot(
    workspace_root: Path,
    *,
    snapshot_id: str,
    operation_type: str,
    target_project_key: str | None,
    reason: str,
    before_files: Mapping[str, str],
    after_files: Mapping[str, str],
) -> Path:
    """Create a workspace/global snapshot capturing multiple files.

    Workspace-level mutations (e.g. project creation, migration) are not
    scoped to a single project file, so they snapshot a set of
    workspace-relative files.

    Layout::

        workspace/.ferumind/global-snapshots/<timestamp>-<snapshot_id>/
            metadata.json
            before/<workspace-relative path>
            after/<workspace-relative path>
            diff.patch

    Returns the path to the created snapshot directory.
    """
    now = datetime.now(UTC)
    ts = now.strftime("%Y%m%dT%H%M%S")
    snapshot_base = contained_path(workspace_root, ".ferumind/global-snapshots")
    _private_directory(snapshot_base)
    snapshot_dir = contained_path(snapshot_base, f"{ts}-{snapshot_id}")
    with _snapshot_construction(snapshot_dir):
        before_dir = snapshot_dir / "before"
        after_dir = snapshot_dir / "after"
        for rel_path, content in before_files.items():
            _write_snapshot_file(before_dir, rel_path, content)
        for rel_path, content in after_files.items():
            _write_snapshot_file(after_dir, rel_path, content)

        diff_parts: list[str] = []
        for rel_path in sorted(set(before_files) | set(after_files)):
            diff_parts.append(
                _generate_diff(before_files.get(rel_path), after_files.get(rel_path), rel_path)
            )
        atomic_write_text(contained_path(snapshot_dir, "diff.patch"), "".join(diff_parts))

        metadata = GlobalSnapshotMetadata(
            id=snapshot_id,
            scope="workspace",
            operation_type=operation_type,
            target_project_key=target_project_key,
            created_at=now.isoformat(),
            reason=reason,
        )
        atomic_write_text(
            contained_path(snapshot_dir, "metadata.json"),
            metadata.model_dump_json(indent=2),
        )

    return snapshot_dir


def create_upload_snapshot(
    project_dir: Path,
    *,
    project_key: str,
    content_path: str,
    content_bytes: bytes,
    metadata_path: str,
    metadata_text: str,
    reason: str,
    snapshot_id: str,
) -> Path:
    """Snapshot a new binary upload plus its metadata sidecar together.

    Both files are new (there is no ``before``), so only ``after/`` is
    populated. The diff file records a byte-count note instead of a text
    diff, since the content file is not text.
    """
    now = datetime.now(UTC)
    ts = now.strftime("%Y%m%dT%H%M%S")
    snapshots_base = contained_path(project_dir, ".ferumind/snapshots")
    _private_directory(snapshots_base)
    snapshot_dir = contained_path(snapshots_base, f"{ts}-{snapshot_id}")
    with _snapshot_construction(snapshot_dir):
        after_dir = snapshot_dir / "after"

        content_target = contained_path(after_dir, content_path)
        _private_directory(content_target.parent)
        atomic_write_bytes(content_target, content_bytes)

        metadata_target = contained_path(after_dir, metadata_path)
        _private_directory(metadata_target.parent)
        atomic_write_text(metadata_target, metadata_text)

        diff_note = (
            f"Binary file added: {content_path} ({len(content_bytes)} bytes)\n"
            f"Metadata sidecar added: {metadata_path}\n"
        )
        atomic_write_text(contained_path(snapshot_dir, "diff.patch"), diff_note)

        metadata = SnapshotMetadata(
            id=snapshot_id,
            project_key=project_key,
            operation_type=reason,
            target_path=content_path,
            created_at=now.isoformat(),
            reason=reason,
            after_sha256=hashlib.sha256(content_bytes).hexdigest(),
            after_size_bytes=len(content_bytes),
        )
        atomic_write_text(
            contained_path(snapshot_dir, "metadata.json"),
            metadata.model_dump_json(indent=2),
        )

    return snapshot_dir


def create_binary_replacement_snapshot(
    project_dir: Path,
    *,
    project_key: str,
    content_path: str,
    before_bytes: bytes,
    after_bytes: bytes,
    reason: str,
    snapshot_id: str,
    metadata_path: str | None = None,
    metadata_before_text: str | None = None,
    metadata_after_text: str | None = None,
) -> Path:
    """Snapshot an in-place rewrite of a binary file, before and after.

    Unlike :func:`create_upload_snapshot`, which records a file that did not
    previously exist, this captures both sides so the prior bytes stay
    recoverable through the normal restore flow. That is what makes
    overwriting user content acceptable: the original is never actually
    destroyed, only superseded.

    The sidecar is included when supplied so a restore returns the file and
    the metadata describing it to a consistent pair.
    """
    now = datetime.now(UTC)
    ts = now.strftime("%Y%m%dT%H%M%S")
    snapshots_base = contained_path(project_dir, ".ferumind/snapshots")
    _private_directory(snapshots_base)
    snapshot_dir = contained_path(snapshots_base, f"{ts}-{snapshot_id}")
    with _snapshot_construction(snapshot_dir):
        for subdir, payload in (("before", before_bytes), ("after", after_bytes)):
            target = contained_path(snapshot_dir / subdir, content_path)
            _private_directory(target.parent)
            atomic_write_bytes(target, payload)

        if metadata_path is not None:
            for subdir, text in (
                ("before", metadata_before_text),
                ("after", metadata_after_text),
            ):
                if text is None:
                    continue
                target = contained_path(snapshot_dir / subdir, metadata_path)
                _private_directory(target.parent)
                atomic_write_text(target, text)

        diff_note = (
            f"Binary file replaced: {content_path} "
            f"({len(before_bytes)} -> {len(after_bytes)} bytes)\n"
        )
        if metadata_path is not None:
            diff_note += f"Metadata sidecar updated: {metadata_path}\n"
        atomic_write_text(contained_path(snapshot_dir, "diff.patch"), diff_note)

        metadata = SnapshotMetadata(
            id=snapshot_id,
            project_key=project_key,
            operation_type=reason,
            target_path=content_path,
            created_at=now.isoformat(),
            reason=reason,
            before_sha256=hashlib.sha256(before_bytes).hexdigest(),
            before_size_bytes=len(before_bytes),
            after_sha256=hashlib.sha256(after_bytes).hexdigest(),
            after_size_bytes=len(after_bytes),
        )
        atomic_write_text(
            contained_path(snapshot_dir, "metadata.json"),
            metadata.model_dump_json(indent=2),
        )

    return snapshot_dir


def _write_snapshot_file(snapshot_subdir: Path, target_path: str, content: str) -> None:
    """Write a snapshot file preserving the relative path structure."""
    if target_path:
        target = contained_path(snapshot_subdir, target_path)
        _private_directory(target.parent)
        atomic_write_text(target, content)
    else:
        _private_directory(snapshot_subdir)


def _generate_diff(before: str | None, after: str | None, target_path: str | None) -> str:
    """Generate a unified diff between before and after content."""
    before_lines = (before or "").splitlines(keepends=True)
    after_lines = (after or "").splitlines(keepends=True)
    path_label = target_path or "unknown"
    diff = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"a/{path_label}",
        tofile=f"b/{path_label}",
    )
    return "".join(diff)


def validate_snapshot_metadata(payload: JsonObject) -> SnapshotMetadata | None:
    try:
        return SnapshotMetadata.model_validate(payload)
    except PydanticValidationError:
        return None


def read_snapshot_metadata(snapshot_dir: Path) -> SnapshotMetadata | None:
    """Read metadata from a snapshot directory."""
    meta_file = snapshot_dir / "metadata.json"
    if not meta_file.is_file():
        return None
    try:
        if meta_file.stat().st_size > MAX_SNAPSHOT_METADATA_BYTES:
            return None
        payload = _coerce_json_object(json.loads(meta_file.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if payload is None:
        return None
    return validate_snapshot_metadata(payload)


def read_snapshot_before_content(
    snapshot_dir: Path,
    target_path: str,
    *,
    expected_sha256: str | None = None,
    expected_size_bytes: int | None = None,
    max_bytes: int | None = None,
) -> str | None:
    """Read and optionally integrity-check a snapshot's before-content.

    Restore callers pass the digest and byte count stored in immutable
    snapshot metadata. Returning ``None`` for a mismatch makes corrupted or
    substituted snapshot payloads unusable rather than silently restoring
    attacker-controlled bytes.
    """
    before_dir = contained_path(snapshot_dir, "before")
    target = contained_path(before_dir, target_path)
    if not target.is_file():
        return None
    try:
        size_bytes = target.stat().st_size
        if max_bytes is not None and size_bytes > max_bytes:
            return None
        if expected_size_bytes is not None and size_bytes != expected_size_bytes:
            return None
        raw = target.read_bytes()
        if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def read_snapshot_after_content(snapshot_dir: Path, target_path: str) -> str | None:
    """Read the after-content of a file from a snapshot."""
    after_dir = contained_path(snapshot_dir, "after")
    target = contained_path(after_dir, target_path)
    if target.is_file():
        return target.read_text(encoding="utf-8")
    return None


def find_snapshot_dir(project_dir: Path, snapshot_id: str) -> Path | None:
    """Find and return the snapshot directory for a given snapshot ID."""
    snapshots_base = contained_path(project_dir, ".ferumind/snapshots")
    if not snapshots_base.is_dir():
        return None
    for entry in snapshots_base.iterdir():
        if not entry.name.endswith(f"-{snapshot_id}"):
            continue
        try:
            safe_entry = contained_path(snapshots_base, entry.name)
        except PathSafetyError:
            continue
        if safe_entry.is_dir():
            return safe_entry
    return None


def record_snapshot_in_db(
    conn: DbConnection,
    *,
    snapshot_id: str,
    project_key: str,
    target_path: str | None,
    snapshot_dir: str,
    reason: str,
    commit: bool = True,
) -> None:
    """Record a snapshot in the snapshots table.

    ``commit=False`` lets a higher-level mutation publish the snapshot row
    and its operation record in one SQLite transaction.
    """
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO snapshots (id, project_key, target_path, snapshot_dir, reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (snapshot_id, project_key, target_path, snapshot_dir, reason, now),
    )
    if commit:
        conn.commit()


def list_snapshots_from_db(
    conn: DbConnection,
    *,
    project_key: str,
    target_path: str | None = None,
    limit: int = 50,
) -> list[SnapshotInfo]:
    """List snapshot registry rows for a project, newest first."""
    params: list[object] = [project_key]
    sql = "SELECT * FROM snapshots WHERE project_key = ?"
    if target_path is not None:
        sql += " AND target_path = ?"
        params.append(target_path)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        SnapshotInfo(
            id=row["id"],
            project_key=row["project_key"],
            target_path=row["target_path"],
            snapshot_dir=row["snapshot_dir"],
            reason=row["reason"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def _coerce_json_object(value: object) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    raw_dict = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw_dict):
        return None
    return cast(JsonObject, value)
