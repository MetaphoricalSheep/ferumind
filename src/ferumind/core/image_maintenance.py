"""Re-run the storage image policy over files already on disk.

Uploads are normalized on the way in (``core.writes``), but a workspace
predates any given policy and the policy itself is meant to be retuned. This
module applies the current :class:`~ferumind.core.images.ImagePolicy` to
existing files so "change the setting and re-run" is a real workflow rather
than a manual chore.

Every rewrite goes through the same guarantees as any other mutation: the
prior bytes are snapshotted before the write, the change is recorded in the
operation log, and the metadata sidecar is updated so its ``sha256`` and
``size_bytes`` keep describing the file they sit beside. Nothing is deleted.

The pass is safe to run repeatedly. :func:`compress_image_for_storage` returns
files unchanged when they are already at or below policy, so a second run over
a converged workspace performs no writes at all.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from ferumind.core.file_io import atomic_write_bytes, atomic_write_text
from ferumind.core.images import ImagePolicy, compress_image_for_storage
from ferumind.core.locks import acquire_project_lock
from ferumind.core.operations import OP_APPLIED, record_operation
from ferumind.core.paths import contained_project_root, is_under_root
from ferumind.core.snapshots import (
    create_binary_replacement_snapshot,
    new_snapshot_id,
    record_snapshot_in_db,
)
from ferumind.core.types import DbConnection, StrictModel
from ferumind.core.writes import upload_metadata_path

#: Extensions worth opening. The pipeline still decides what it can actually
#: handle; this only avoids reading every unrelated byte in the project.
_CANDIDATE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"})

_OPERATION_TYPE = "compress_image"


class ImageCompressionEntry(StrictModel):
    """What happened to one file."""

    path: str
    before_bytes: int
    after_bytes: int
    changed: bool
    reason: str | None = None
    sidecar_updated: bool = False
    operation_id: str | None = None
    snapshot_id: str | None = None
    error: str | None = None

    @property
    def saved_bytes(self) -> int:
        return self.before_bytes - self.after_bytes


class ImageCompressionReport(StrictModel):
    """Aggregate outcome of one pass over a project."""

    project_key: str
    dry_run: bool
    scanned: int
    changed: int
    skipped: int
    failed: int
    bytes_before: int
    bytes_after: int
    entries: list[ImageCompressionEntry]

    @property
    def saved_bytes(self) -> int:
        return self.bytes_before - self.bytes_after


def _iter_candidate_files(project_root: Path) -> list[Path]:
    """Collect image candidates, skipping Ferumind's own internal state.

    ``.ferumind/`` holds snapshots — including prior copies of the very files
    being rewritten. Compressing those would destroy the recovery path this
    pass depends on, so anything dot-prefixed is excluded outright.
    """
    found: list[Path] = []
    for path in sorted(project_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part.startswith(".") for part in path.relative_to(project_root).parts):
            continue
        if path.suffix.lower() not in _CANDIDATE_SUFFIXES:
            continue
        if not is_under_root(path, project_root):
            continue
        found.append(path)
    return found


def _update_sidecar(
    sidecar: Path,
    *,
    sha256: str,
    size_bytes: int,
    mime_type: str | None,
    original_size_bytes: int,
    width: int | None,
    height: int | None,
    now: str,
) -> str | None:
    """Rewrite the sidecar's stored facts to match the new bytes.

    Returns the new sidecar text, or ``None`` when there is no sidecar or it
    is not a JSON object this code should be editing. Only the keys that
    describe the bytes are touched; agent-authored keys are preserved.
    """
    if not sidecar.is_file():
        return None
    try:
        loaded = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None

    payload: dict[str, object] = dict(loaded)  # pyright: ignore[reportUnknownArgumentType]
    payload["sha256"] = sha256
    payload["size_bytes"] = size_bytes
    if mime_type is not None:
        payload["mime_type"] = mime_type
    payload["image_compression"] = {
        "source_size_bytes": original_size_bytes,
        "width": width,
        "height": height,
        "compressed_at": now,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def compress_project_images(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    policy: ImagePolicy | None = None,
    dry_run: bool = False,
) -> ImageCompressionReport:
    """Apply *policy* to every image already stored in one project.

    With ``dry_run`` the files are compressed in memory and measured but
    nothing is written, so the report shows exactly what a real run would do.
    """
    active = (policy or ImagePolicy()).validated()
    project_root = contained_project_root(workspace_root, project_key)

    entries: list[ImageCompressionEntry] = []
    bytes_before = 0
    bytes_after = 0
    changed = skipped = failed = 0

    with acquire_project_lock(project_root, project_key):
        for absolute in _iter_candidate_files(project_root):
            rel = absolute.relative_to(project_root).as_posix()
            try:
                raw = absolute.read_bytes()
            except OSError as exc:
                failed += 1
                entries.append(
                    ImageCompressionEntry(
                        path=rel,
                        before_bytes=0,
                        after_bytes=0,
                        changed=False,
                        error=f"unreadable: {type(exc).__name__}",
                    )
                )
                continue

            bytes_before += len(raw)
            try:
                result = compress_image_for_storage(raw, policy=active)
            except Exception as exc:  # one bad file must not abort the pass
                failed += 1
                bytes_after += len(raw)
                entries.append(
                    ImageCompressionEntry(
                        path=rel,
                        before_bytes=len(raw),
                        after_bytes=len(raw),
                        changed=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            if not result.changed:
                skipped += 1
                bytes_after += len(raw)
                entries.append(
                    ImageCompressionEntry(
                        path=rel,
                        before_bytes=len(raw),
                        after_bytes=len(raw),
                        changed=False,
                        reason=result.reason,
                    )
                )
                continue

            bytes_after += result.size_bytes
            changed += 1
            if dry_run:
                entries.append(
                    ImageCompressionEntry(
                        path=rel,
                        before_bytes=len(raw),
                        after_bytes=result.size_bytes,
                        changed=True,
                    )
                )
                continue

            entries.append(
                _apply_one(
                    conn,
                    project_root=project_root,
                    project_key=project_key,
                    absolute=absolute,
                    rel=rel,
                    raw=raw,
                    new_bytes=result.data,
                    mime_type=result.mime_type,
                    width=result.width,
                    height=result.height,
                )
            )

    return ImageCompressionReport(
        project_key=project_key,
        dry_run=dry_run,
        scanned=len(entries),
        changed=changed,
        skipped=skipped,
        failed=failed,
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        entries=entries,
    )


def _apply_one(
    conn: DbConnection,
    *,
    project_root: Path,
    project_key: str,
    absolute: Path,
    rel: str,
    raw: bytes,
    new_bytes: bytes,
    mime_type: str | None,
    width: int | None,
    height: int | None,
) -> ImageCompressionEntry:
    """Snapshot, write, update the sidecar, and log one rewrite atomically.

    Any failure rolls the database back and restores the original bytes, so a
    partial rewrite never survives.
    """
    now = datetime.now(UTC).isoformat()
    sha256 = hashlib.sha256(new_bytes).hexdigest()
    sidecar_rel = upload_metadata_path(rel)
    sidecar = project_root / sidecar_rel
    sidecar_before = sidecar.read_text(encoding="utf-8") if sidecar.is_file() else None
    sidecar_after = _update_sidecar(
        sidecar,
        sha256=sha256,
        size_bytes=len(new_bytes),
        mime_type=mime_type,
        original_size_bytes=len(raw),
        width=width,
        height=height,
        now=now,
    )

    snapshot_id = new_snapshot_id()
    snapshot_dir = create_binary_replacement_snapshot(
        project_root,
        project_key=project_key,
        content_path=rel,
        before_bytes=raw,
        after_bytes=new_bytes,
        reason=_OPERATION_TYPE,
        snapshot_id=snapshot_id,
        metadata_path=sidecar_rel if sidecar_before is not None else None,
        metadata_before_text=sidecar_before,
        metadata_after_text=sidecar_after,
    )

    try:
        atomic_write_bytes(absolute, new_bytes)
        if sidecar_after is not None:
            atomic_write_text(sidecar, sidecar_after)
        record_snapshot_in_db(
            conn,
            snapshot_id=snapshot_id,
            project_key=project_key,
            target_path=rel,
            snapshot_dir=str(snapshot_dir),
            reason=_OPERATION_TYPE,
            commit=False,
        )
        operation_id = record_operation(
            conn,
            project_key=project_key,
            operation_type=_OPERATION_TYPE,
            tool_name=_OPERATION_TYPE,
            target_path=rel,
            request_json={
                "size_bytes_before": len(raw),
                "size_bytes_after": len(new_bytes),
                "mime_type": mime_type,
                "width": width,
                "height": height,
            },
            base_sha256=hashlib.sha256(raw).hexdigest(),
            after_sha256=sha256,
            snapshot_id=snapshot_id,
            state=OP_APPLIED,
            commit=False,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        atomic_write_bytes(absolute, raw)
        if sidecar_before is not None:
            atomic_write_text(sidecar, sidecar_before)
        raise

    return ImageCompressionEntry(
        path=rel,
        before_bytes=len(raw),
        after_bytes=len(new_bytes),
        changed=True,
        sidecar_updated=sidecar_after is not None,
        operation_id=operation_id,
        snapshot_id=snapshot_id,
    )
