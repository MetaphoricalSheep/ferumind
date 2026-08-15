"""Uploads: how non-Markdown bytes enter a project's ``library/``.

Three doors onto one write path. ``upload_library_file`` takes base64 in a
single call; ``start_library_file_upload`` / ``append_upload_chunk`` /
``finalize_library_file_upload`` / ``discard_upload`` carry a larger file in
pieces through a pending session; and ``upload_library_file(s)_from_chatgpt``
has the server fetch ChatGPT-resolved URLs itself, so the model never
reproduces file bytes as text. All of them end in
:func:`_write_uploaded_file`, which is what makes the stored result identical
whichever door the bytes came through: extension denylist, fail-closed
collision check under the project lock, image normalization, a ``{stem}.json``
metadata sidecar, a snapshot, and an operation-log entry.

Uploaded files are deliberately **not** indexed — search and ``get_context``
are Markdown-only (spec-mcp §5.4), and a non-Markdown upload never reaches the
indexer.

Chunk bytes are staged by :mod:`ferumind.core.uploads`, a separate module this
one calls as ``upload_staging.<fn>(...)``: staging is scratch storage under
``.ferumind/``, not published content, and keeping it at arm's length keeps
that boundary visible. Bounds live in :mod:`ferumind.core.write_limits`; the
path guard and folder canonicalization live in
:mod:`ferumind.core.write_common`.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import logging
import mimetypes
import re
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast

from pydantic import BaseModel, ConfigDict, Field

from ferumind.core import uploads as upload_staging
from ferumind.core.errors import (
    ContentHashMismatchError,
    DocumentExistsError,
    DownloadTimeoutError,
    FerumindError,
    FileTooLargeError,
    InvalidOperationError,
    OperationNotFoundError,
    PatchExpiredError,
    PatchProjectMismatchError,
    UnknownFolderError,
    UploadIncompleteError,
    ValidationError,
)
from ferumind.core.file_io import atomic_write_bytes, atomic_write_text
from ferumind.core.folders import folder_of
from ferumind.core.images import ImagePolicy, compress_image_for_storage
from ferumind.core.locks import acquire_project_lock
from ferumind.core.operations import (
    OP_APPLIED,
    OP_DISCARDED,
    OP_EXPIRED,
    OP_FAILED,
    OP_PENDING,
    OperationRecord,
    get_operation,
    is_expired,
    mark_operation_state,
    new_proposal_id,
    record_operation,
    sweep_expired_proposals,
)
from ferumind.core.paths import contained_project_root
from ferumind.core.remote_fetch import DEFAULT_TOTAL_TIMEOUT, fetch_remote_file
from ferumind.core.snapshots import (
    create_upload_snapshot,
    new_snapshot_id,
    record_snapshot_in_db,
)
from ferumind.core.types import DbConnection, JsonObject
from ferumind.core.write_common import canonical_folder_path, validate_path_safe
from ferumind.core.write_limits import (
    BLOCKED_UPLOAD_EXTENSIONS,
    MAX_CHATGPT_BATCH_BYTES,
    MAX_CHATGPT_BATCH_SECONDS,
    MAX_CHATGPT_FILES_PER_CALL,
    MAX_CHUNK_BYTES,
    MAX_MIME_TYPE_CHARS,
    MAX_PENDING_UPLOAD_BYTES_PER_PROJECT,
    MAX_PENDING_UPLOAD_SESSIONS_PER_PROJECT,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_CHUNKS,
    MAX_UPLOAD_FILENAME_BYTES,
    MAX_UPLOAD_METADATA_BYTES,
    upload_metadata_path,
)

logger = logging.getLogger(__name__)

_SHA256_RE: Final = re.compile(r"^[0-9a-fA-F]{64}$")

#: Characters a POSIX filesystem accepts and a synced one does not. ``:`` is
#: the one that matters for safety — on NTFS ``notes.txt:payload.exe`` names
#: an alternate data stream rather than a file, so the extension the denylist
#: inspected is not the extension that ends up executable. The rest cannot
#: exist on Windows at all; refusing them at upload beats writing a file the
#: owner's own sync client will later fail on or silently mangle.
_RESERVED_FILENAME_CHARACTERS: Final = frozenset(':<>"|?*')

#: Reserved on Windows with or without an extension: ``NUL.txt`` is the
#: device, not a text file. Compared against the stem, case-folded.
_RESERVED_DEVICE_NAMES: Final[frozenset[str]] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in range(1, 10)}
    | {f"lpt{digit}" for digit in range(1, 10)}
)


def _validated_metadata(metadata: JsonObject | None) -> JsonObject:
    value = dict(metadata or {})
    encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_UPLOAD_METADATA_BYTES:
        raise FileTooLargeError(
            f"Upload metadata exceeds the {MAX_UPLOAD_METADATA_BYTES}-byte limit",
            details={"max_bytes": MAX_UPLOAD_METADATA_BYTES, "scope": "metadata"},
        )
    return value


def _validated_mime_type(mime_type: str | None) -> str | None:
    if mime_type is None:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in mime_type):
        raise ValidationError("mime_type must not contain control characters")
    value = mime_type.strip()
    if not value:
        return None
    if len(value) > MAX_MIME_TYPE_CHARS or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValidationError(
            f"mime_type must be at most {MAX_MIME_TYPE_CHARS} characters "
            "and contain no control characters"
        )
    return value


def _validate_upload_filename(filename: str) -> str:
    """Validate a bare filename (no path separators, no traversal)."""
    if filename != filename.strip():
        raise ValidationError("filename must not have leading or trailing whitespace")
    name = filename
    if not name:
        raise ValidationError("filename must not be empty")
    if "/" in name or "\\" in name or name in (".", ".."):
        raise ValidationError(f"filename must be a bare filename, not a path: {filename!r}")
    if name.startswith(".") or any(
        ord(character) < 32 or ord(character) == 127 for character in name
    ):
        raise ValidationError("filename must not be hidden or contain control characters")
    if not _RESERVED_FILENAME_CHARACTERS.isdisjoint(name):
        raise ValidationError(
            "filename must not contain any of "
            f"{''.join(sorted(_RESERVED_FILENAME_CHARACTERS))} — they name "
            "alternate data streams or cannot be synced to other filesystems"
        )
    # Windows strips trailing dots on create, so "payload.exe." reaches disk
    # as "payload.exe" — and PurePath reports its suffix as "." rather than
    # ".exe", which is what walks it past BLOCKED_UPLOAD_EXTENSIONS. Trailing
    # whitespace is already refused by the strip() check above.
    if name.endswith("."):
        raise ValidationError("filename must not end in a dot")
    if name.partition(".")[0].casefold() in _RESERVED_DEVICE_NAMES:
        raise ValidationError(f"filename {filename!r} is a reserved device name")
    if len(name.encode("utf-8")) > MAX_UPLOAD_FILENAME_BYTES:
        raise ValidationError(
            f"filename exceeds the {MAX_UPLOAD_FILENAME_BYTES}-byte filesystem limit"
        )
    return name


def _validate_upload_folder_path(folder_path: str) -> str:
    """Validate folder_path is under library/ (uploads always land there)."""
    folder_path = canonical_folder_path(folder_path, allow_empty=True)
    if not folder_path:
        return "library"
    first_segment = folder_path.split("/", 1)[0]
    if first_segment != "library":
        msg = f"folder_path {folder_path!r} must be under library/ — uploads always land there"
        raise UnknownFolderError(msg, details={"allowed_folders": ["library"]})
    return folder_path


class UploadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    snapshot_id: str
    path: str
    metadata_path: str
    folder: str | None = None
    sha256: str
    size_bytes: int
    mime_type: str | None = None


def _write_uploaded_file(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    filename: str,
    raw: bytes,
    folder_path: str,
    mime_type: str | None,
    metadata: JsonObject,
    operation_type: str,
    project_lock_held: bool = False,
    terminal_operation_id: str | None = None,
    image_policy: ImagePolicy | None = None,
) -> UploadResult:
    """Shared machinery: fail-closed collision check, write + snapshot + oplog.

    Used by both the one-shot ``upload_library_file`` and
    ``finalize_library_file_upload`` (chunked path) so their on-disk result
    is identical regardless of how the bytes arrived.

    Raster uploads are normalized to the storage policy *before* anything is
    hashed or recorded, so the sidecar, the snapshot, and the operation log
    all describe the bytes that were actually stored. The full-resolution
    original is deliberately not retained: no transport in front of this
    server can carry one, and keeping it would defeat the normalization.
    """
    filename = _validate_upload_filename(filename)
    folder_path = _validate_upload_folder_path(folder_path)
    mime_type = _validated_mime_type(mime_type)
    metadata = _validated_metadata(metadata)

    compression = compress_image_for_storage(
        raw, mime_type=mime_type, policy=image_policy or ImagePolicy()
    )
    raw = compression.data
    if compression.changed:
        mime_type = compression.mime_type
    doc_rel = f"{folder_path}/{filename}"
    metadata_rel = upload_metadata_path(doc_rel)

    project_dir = contained_project_root(workspace_root, project_key)
    target_file = validate_path_safe(
        workspace_root, project_key, doc_rel, extensions_denylist=BLOCKED_UPLOAD_EXTENSIONS
    )
    metadata_file = validate_path_safe(
        workspace_root, project_key, metadata_rel, extensions_denylist=BLOCKED_UPLOAD_EXTENSIONS
    )

    lock_context = (
        contextlib.nullcontext()
        if project_lock_held
        else acquire_project_lock(project_dir, project_key)
    )
    with lock_context:
        if target_file.exists() or metadata_file.exists():
            raise DocumentExistsError(
                f"A file already exists at {doc_rel}",
                details={
                    "path": doc_rel,
                    "recommended_action": "Choose a different filename or folder_path.",
                },
            )

        sha256 = hashlib.sha256(raw).hexdigest()
        now = datetime.now(UTC).isoformat()
        protected_fields: JsonObject = {
            "original_filename": filename,
            "uploaded_at": now,
            "uploaded_by_tool": operation_type,
            "sha256": sha256,
            "size_bytes": len(raw),
            "mime_type": mime_type,
        }
        if compression.changed:
            # Provenance for a stored copy that is not what the caller sent.
            protected_fields["image_compression"] = {
                "source_size_bytes": compression.original_size_bytes,
                "source_width": compression.original_width,
                "source_height": compression.original_height,
                "width": compression.width,
                "height": compression.height,
                "compressed_at": now,
            }
        merged_metadata: JsonObject = {**metadata, **protected_fields}
        metadata_text = json.dumps(merged_metadata, indent=2, sort_keys=True) + "\n"
        if len(metadata_text.encode("utf-8")) > MAX_UPLOAD_METADATA_BYTES:
            raise FileTooLargeError(
                f"Upload metadata exceeds the {MAX_UPLOAD_METADATA_BYTES}-byte limit",
                details={"max_bytes": MAX_UPLOAD_METADATA_BYTES, "scope": "metadata"},
            )

        snapshot_id = new_snapshot_id()
        snapshot_dir = create_upload_snapshot(
            project_dir,
            project_key=project_key,
            content_path=doc_rel,
            content_bytes=raw,
            metadata_path=metadata_rel,
            metadata_text=metadata_text,
            reason=operation_type,
            snapshot_id=snapshot_id,
        )

        files_published = False
        try:
            atomic_write_bytes(target_file, raw)
            atomic_write_text(metadata_file, metadata_text)
            files_published = True
            record_snapshot_in_db(
                conn,
                snapshot_id=snapshot_id,
                project_key=project_key,
                target_path=doc_rel,
                snapshot_dir=str(snapshot_dir),
                reason=operation_type,
                commit=False,
            )
            op_id = record_operation(
                conn,
                project_key=project_key,
                operation_type=operation_type,
                tool_name=operation_type,
                target_path=doc_rel,
                request_json={
                    "filename": filename,
                    "folder_path": folder_path,
                    "metadata_path": metadata_rel,
                    "size_bytes": len(raw),
                    "mime_type": mime_type,
                    "sha256": sha256,
                },
                after_sha256=sha256,
                snapshot_id=snapshot_id,
                state=OP_APPLIED,
                commit=False,
            )
            if terminal_operation_id is not None:
                mark_operation_state(
                    conn,
                    terminal_operation_id,
                    OP_APPLIED,
                    commit=False,
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            # Both targets were proven absent while holding the project lock.
            # Compensate any partial pair or durable-bookkeeping failure so a
            # failed call never leaves content without its audit records.
            target_file.unlink(missing_ok=True)
            metadata_file.unlink(missing_ok=True)
            try:
                shutil.rmtree(snapshot_dir)
            except OSError as cleanup_error:
                logger.error(
                    "Upload rollback could not remove its snapshot (type=%s, published=%s)",
                    type(cleanup_error).__name__,
                    files_published,
                )
            raise

    return UploadResult(
        operation_id=op_id,
        snapshot_id=snapshot_id,
        path=doc_rel,
        metadata_path=metadata_rel,
        folder=folder_of(doc_rel),
        sha256=sha256,
        size_bytes=len(raw),
        mime_type=mime_type,
    )


def upload_library_file(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    filename: str,
    content_base64: str,
    folder_path: str = "library",
    mime_type: str | None = None,
    metadata: JsonObject | None = None,
    image_policy: ImagePolicy | None = None,
) -> UploadResult:
    """Upload a binary file into library/, with a ``{stem}.json`` metadata sidecar.

    Always lands under ``library/``; ``folder_path`` may nest below it, like
    ``create_document``. Fails closed on a filename collision — no silent
    overwrite. Scripts/executables are blocked by extension
    (``BLOCKED_UPLOAD_EXTENSIONS``); the decoded payload is capped at
    ``MAX_CHUNK_BYTES`` since this whole call has to fit in one tool call —
    a file too large for that needs ``start_library_file_upload`` instead.
    The metadata sidecar is agent-authored: a few protected audit fields are
    stamped by the server (sha256, size, upload time, mime type), and any
    additional keys the caller supplies are kept as-is. Not indexed for
    search_project/list_tree (Markdown-only today).

    One-shot; for files that need to arrive in smaller pieces, see
    ``start_library_file_upload`` / ``append_upload_chunk`` /
    ``finalize_library_file_upload``.
    """
    name = _validate_upload_filename(filename)
    folder_path = _validate_upload_folder_path(folder_path)
    metadata_value = _validated_metadata(metadata)
    mime_type = _validated_mime_type(mime_type)

    max_encoded_chars = ((MAX_CHUNK_BYTES + 2) // 3) * 4
    if len(content_base64) > max_encoded_chars:
        raise FileTooLargeError(
            "Base64 payload exceeds the single-call upload limit",
            details={"max_bytes": MAX_CHUNK_BYTES, "scope": "file"},
        )
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except binascii.Error as exc:
        raise ValidationError(f"content_base64 is not valid base64: {exc}") from exc

    if len(raw) > MAX_CHUNK_BYTES:
        msg = f"File is {len(raw)} bytes, exceeds the {MAX_CHUNK_BYTES}-byte single-call limit"
        raise FileTooLargeError(
            msg,
            details={
                "size_bytes": len(raw),
                "max_bytes": MAX_CHUNK_BYTES,
                "scope": "file",
                "recommended_action": (
                    "Use start_library_file_upload/append_upload_chunk/"
                    "finalize_library_file_upload for files this large."
                ),
            },
        )

    return _write_uploaded_file(
        conn,
        workspace_root,
        project_key,
        filename=name,
        raw=raw,
        folder_path=folder_path,
        mime_type=mime_type,
        metadata=metadata_value,
        operation_type="upload_library_file",
        image_policy=image_policy,
    )


# ── Chunked upload (start / append / finalize / discard) ────────────────────


#: Chunked upload sessions use the same pending/applied/discarded/expired
#: lifecycle and 24h TTL as patch proposals (operations.py), keyed by
#: upload_id instead of a patch operation_id.
UPLOAD_SESSION_OP_TYPE: Final = "upload_session"
UPLOAD_SESSION_TTL: Final = timedelta(hours=24)


class UploadSessionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: str
    expires_at: str
    chunk_size_hint: int = MAX_CHUNK_BYTES


class ChunkAppendResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: str
    received_chunks: int
    total_chunks: int
    received_bytes: int


def _get_upload_session(conn: DbConnection, project_key: str, upload_id: str) -> OperationRecord:
    op = get_operation(conn, upload_id)
    if op is None:
        raise OperationNotFoundError(f"Upload {upload_id} not found")
    if op.operation_type != UPLOAD_SESSION_OP_TYPE:
        raise InvalidOperationError(f"Operation {upload_id} is not an upload session")
    if op.project_key != project_key:
        msg = (
            f"Upload {upload_id} belongs to project {op.project_key!r}, "
            f"not the asserted project {project_key!r}"
        )
        raise PatchProjectMismatchError(msg)
    return op


def _ensure_upload_session_active(
    conn: DbConnection,
    project_dir: Path,
    op: OperationRecord,
) -> None:
    """Raise if the session is expired or not pending; expire it in place if due."""
    if op.state == OP_PENDING and is_expired(op):
        upload_staging.remove_staging_dir(project_dir, op.id)
        mark_operation_state(conn, op.id, OP_EXPIRED)
        raise PatchExpiredError(
            f"Upload {op.id} expired; upload sessions are valid for 24 h. Start a new upload.",
            details={"expires_at": op.expires_at},
        )
    if op.state != OP_PENDING:
        msg = f"Upload {op.id} has state {op.state!r}, expected pending"
        raise InvalidOperationError(msg, details={"state": op.state})


def _expire_upload_sessions(
    conn: DbConnection,
    project_dir: Path,
    project_key: str,
) -> None:
    """Expire pending upload rows and remove their bounded staging data."""
    now = datetime.now(UTC).isoformat()
    rows = conn.execute(
        """SELECT id FROM operations
           WHERE project_key = ? AND operation_type = ? AND state = ?
             AND expires_at IS NOT NULL AND expires_at < ?""",
        (project_key, UPLOAD_SESSION_OP_TYPE, OP_PENDING, now),
    ).fetchall()
    for row in rows:
        upload_staging.remove_staging_dir(project_dir, row["id"])
    sweep_expired_proposals(conn, project_key)


def _pending_upload_reservations(
    conn: DbConnection,
    project_key: str,
) -> tuple[int, int]:
    """Return active upload-session count and declared bytes, failing closed on bad rows."""
    rows = conn.execute(
        """SELECT request_json FROM operations
           WHERE project_key = ? AND operation_type = ? AND state = ?""",
        (project_key, UPLOAD_SESSION_OP_TYPE, OP_PENDING),
    ).fetchall()
    reserved_bytes = 0
    for row in rows:
        try:
            raw_payload: object = json.loads(row["request_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise InvalidOperationError("Pending upload session record is malformed") from exc
        if not isinstance(raw_payload, dict):
            raise InvalidOperationError("Pending upload session record is malformed")
        payload = cast(dict[object, object], raw_payload)
        size = payload.get("total_size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise InvalidOperationError("Pending upload session record is malformed")
        reserved_bytes += size
    return len(rows), reserved_bytes


def start_library_file_upload(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    filename: str,
    total_size: int,
    total_chunks: int,
    folder_path: str = "library",
    mime_type: str | None = None,
    metadata: JsonObject | None = None,
    expected_sha256: str | None = None,
) -> UploadSessionResult:
    """Start a chunked upload session for a library file arriving in pieces.

    Declares the file's identity and size up front; bytes then arrive via
    repeated ``append_upload_chunk`` calls (each capped at
    ``MAX_CHUNK_BYTES``) and are assembled by
    ``finalize_library_file_upload``. Multiple sessions for different files
    may be open at once — each gets its own ``upload_id`` and independent
    chunk staging area, so uploads can be interleaved freely.
    """
    name = _validate_upload_filename(filename)
    folder_path = _validate_upload_folder_path(folder_path)
    metadata_value = _validated_metadata(metadata)
    mime_type = _validated_mime_type(mime_type)
    if expected_sha256 is not None:
        if not _SHA256_RE.fullmatch(expected_sha256):
            raise ValidationError("expected_sha256 must be exactly 64 hexadecimal characters")
        expected_sha256 = expected_sha256.lower()

    if isinstance(total_chunks, bool) or total_chunks < 1:
        raise ValidationError("total_chunks must be >= 1")
    if total_chunks > MAX_UPLOAD_CHUNKS:
        raise ValidationError(
            f"total_chunks {total_chunks} exceeds the maximum of {MAX_UPLOAD_CHUNKS}",
            details={"total_chunks": total_chunks, "max_chunks": MAX_UPLOAD_CHUNKS},
        )
    if isinstance(total_size, bool) or total_size < 0:
        raise ValidationError("total_size must be >= 0")
    if total_size > MAX_UPLOAD_BYTES:
        raise FileTooLargeError(
            f"Declared total_size {total_size} exceeds the {MAX_UPLOAD_BYTES}-byte upload limit",
            details={"size_bytes": total_size, "max_bytes": MAX_UPLOAD_BYTES, "scope": "file"},
        )
    if total_size > total_chunks * MAX_CHUNK_BYTES:
        raise ValidationError(
            "Declared total_size cannot fit in total_chunks at the per-chunk size limit",
            details={
                "total_size": total_size,
                "total_chunks": total_chunks,
                "max_chunk_bytes": MAX_CHUNK_BYTES,
            },
        )

    doc_rel = f"{folder_path}/{name}"
    metadata_rel = upload_metadata_path(doc_rel)
    expires_at = (datetime.now(UTC) + UPLOAD_SESSION_TTL).isoformat()
    request_json: JsonObject = {
        "filename": name,
        "folder_path": folder_path,
        "mime_type": mime_type,
        "metadata": metadata_value,
        "total_size": total_size,
        "total_chunks": total_chunks,
        "expected_sha256": expected_sha256,
    }
    upload_id = new_proposal_id()
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        _expire_upload_sessions(conn, project_dir, project_key)
        target_file = validate_path_safe(
            workspace_root,
            project_key,
            doc_rel,
            extensions_denylist=BLOCKED_UPLOAD_EXTENSIONS,
        )
        metadata_file = validate_path_safe(
            workspace_root,
            project_key,
            metadata_rel,
            extensions_denylist=BLOCKED_UPLOAD_EXTENSIONS,
        )
        if target_file.exists() or metadata_file.exists():
            raise DocumentExistsError(
                f"A file already exists at {doc_rel}",
                details={
                    "path": doc_rel,
                    "recommended_action": "Choose a different filename or folder_path.",
                },
            )
        duplicate = conn.execute(
            """SELECT id FROM operations
               WHERE project_key = ? AND operation_type = ? AND state = ?
                 AND target_path = ?
               LIMIT 1""",
            (project_key, UPLOAD_SESSION_OP_TYPE, OP_PENDING, doc_rel),
        ).fetchone()
        if duplicate is not None:
            raise DocumentExistsError(
                f"An upload is already pending for {doc_rel}",
                details={
                    "path": doc_rel,
                    "recommended_action": (
                        "Finalize or discard the existing upload before starting another."
                    ),
                },
            )
        pending_sessions, reserved_bytes = _pending_upload_reservations(conn, project_key)
        if pending_sessions >= MAX_PENDING_UPLOAD_SESSIONS_PER_PROJECT:
            raise ValidationError(
                "Too many pending upload sessions for this project",
                details={"max_pending_sessions": MAX_PENDING_UPLOAD_SESSIONS_PER_PROJECT},
            )
        if reserved_bytes + total_size > MAX_PENDING_UPLOAD_BYTES_PER_PROJECT:
            raise FileTooLargeError(
                "Pending uploads exceed the project staging reservation limit",
                details={
                    "reserved_bytes": reserved_bytes,
                    "requested_bytes": total_size,
                    "max_bytes": MAX_PENDING_UPLOAD_BYTES_PER_PROJECT,
                    "scope": "project_upload_staging",
                },
            )
        record_operation(
            conn,
            project_key=project_key,
            operation_type=UPLOAD_SESSION_OP_TYPE,
            tool_name="start_library_file_upload",
            target_path=doc_rel,
            request_json=request_json,
            state=OP_PENDING,
            expires_at=expires_at,
            operation_id=upload_id,
        )
    return UploadSessionResult(upload_id=upload_id, expires_at=expires_at)


def append_upload_chunk(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    upload_id: str,
    chunk_index: int,
    chunk_base64: str,
) -> ChunkAppendResult:
    """Append one chunk of a pending upload session.

    Idempotent per ``chunk_index``: resending the same index overwrites it,
    so a retried chunk is safe. Enforces ``MAX_CHUNK_BYTES`` per chunk and
    the session's declared ``total_size`` cumulatively.
    """
    max_encoded_chars = ((MAX_CHUNK_BYTES + 2) // 3) * 4
    if len(chunk_base64) > max_encoded_chars:
        raise FileTooLargeError(
            "Base64 chunk exceeds the per-chunk upload limit",
            details={"max_bytes": MAX_CHUNK_BYTES, "scope": "chunk"},
        )
    try:
        raw = base64.b64decode(chunk_base64, validate=True)
    except binascii.Error as exc:
        raise ValidationError(f"chunk_base64 is not valid base64: {exc}") from exc

    if len(raw) > MAX_CHUNK_BYTES:
        raise FileTooLargeError(
            f"Chunk is {len(raw)} bytes, exceeds the {MAX_CHUNK_BYTES}-byte per-chunk limit",
            details={"size_bytes": len(raw), "max_bytes": MAX_CHUNK_BYTES, "scope": "chunk"},
        )

    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        op = _get_upload_session(conn, project_key, upload_id)
        _ensure_upload_session_active(conn, project_dir, op)
        total_chunks = op.request_json.get("total_chunks")
        declared_total_size = op.request_json.get("total_size")
        if not isinstance(total_chunks, int) or not isinstance(declared_total_size, int):
            raise InvalidOperationError(f"Upload {upload_id} session record is malformed")
        if not (0 <= chunk_index < total_chunks):
            raise ValidationError(f"chunk_index {chunk_index} out of range [0, {total_chunks})")

        upload_staging.write_chunk(project_dir, upload_id, chunk_index, raw)
        received = upload_staging.received_chunk_indices(project_dir, upload_id)
        received_bytes = upload_staging.staged_size_bytes(project_dir, upload_id)

        if received_bytes > declared_total_size:
            upload_staging.remove_staging_dir(project_dir, upload_id)
            mark_operation_state(conn, upload_id, OP_FAILED)
            raise FileTooLargeError(
                f"Received {received_bytes} bytes, exceeds the declared "
                f"total_size {declared_total_size}",
                details={
                    "size_bytes": received_bytes,
                    "max_bytes": declared_total_size,
                    "scope": "file",
                },
            )

    return ChunkAppendResult(
        upload_id=upload_id,
        received_chunks=len(received),
        total_chunks=total_chunks,
        received_bytes=received_bytes,
    )


def finalize_library_file_upload(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    upload_id: str,
    image_policy: ImagePolicy | None = None,
) -> UploadResult:
    """Assemble a completed chunked upload and write it (same result shape as upload_library_file).

    Fails with ``UPLOAD_INCOMPLETE`` if any chunk is missing, a plain
    ``VALIDATION_ERROR`` if the assembled size doesn't match what
    ``start_library_file_upload`` declared, and ``CONTENT_HASH_MISMATCH`` if
    an ``expected_sha256`` was declared and doesn't match — the assembled
    bytes are discarded rather than written in every failure case.
    """
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        op = _get_upload_session(conn, project_key, upload_id)
        _ensure_upload_session_active(conn, project_dir, op)
        total_chunks = op.request_json.get("total_chunks")
        total_size = op.request_json.get("total_size")
        filename = op.request_json.get("filename")
        folder_path = op.request_json.get("folder_path")
        mime_type = op.request_json.get("mime_type")
        metadata = op.request_json.get("metadata")
        expected_sha256 = op.request_json.get("expected_sha256")
        if (
            not isinstance(total_chunks, int)
            or not isinstance(total_size, int)
            or not isinstance(filename, str)
            or not isinstance(folder_path, str)
            or not isinstance(metadata, dict)
            or total_chunks < 1
            or total_chunks > MAX_UPLOAD_CHUNKS
        ):
            raise InvalidOperationError(f"Upload {upload_id} session record is malformed")

        received = upload_staging.received_chunk_indices(project_dir, upload_id)
        missing = [i for i in range(total_chunks) if i not in received]
        if missing:
            raise UploadIncompleteError(
                f"Upload {upload_id} is missing {len(missing)} of {total_chunks} chunks",
                details={
                    "missing_chunk_indices": list(missing[:50]),
                    "total_chunks": total_chunks,
                },
            )
        raw = upload_staging.assemble_chunks(project_dir, upload_id, total_chunks)

        if len(raw) != total_size:
            raise ValidationError(
                f"Assembled upload is {len(raw)} bytes, does not match declared "
                f"total_size {total_size}",
                details={"assembled_bytes": len(raw), "declared_total_size": total_size},
            )

        if isinstance(expected_sha256, str) and expected_sha256:
            actual_sha256 = hashlib.sha256(raw).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ContentHashMismatchError(
                    "Assembled upload does not match expected_sha256 — likely corrupted in transit",
                    details={
                        "expected_sha256": expected_sha256,
                        "actual_sha256": actual_sha256,
                    },
                )

        result = _write_uploaded_file(
            conn,
            workspace_root,
            project_key,
            filename=filename,
            raw=raw,
            folder_path=folder_path,
            mime_type=mime_type if isinstance(mime_type, str) else None,
            metadata=metadata,
            operation_type="finalize_library_file_upload",
            project_lock_held=True,
            terminal_operation_id=upload_id,
            image_policy=image_policy,
        )
        try:
            upload_staging.remove_staging_dir(project_dir, upload_id)
        except OSError as exc:
            # Content publication and the terminal session state already
            # succeeded. Treat redundant staging cleanup as maintenance work
            # rather than returning a false failure for a completed upload.
            logger.warning(
                "Completed upload staging cleanup failed (type=%s)",
                type(exc).__name__,
            )

    return result


class DiscardUploadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: str
    path: str | None = None
    state: str = OP_DISCARDED


def discard_upload(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    upload_id: str,
) -> DiscardUploadResult:
    """Abandon a pending upload session and delete its staged chunks."""
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        op = _get_upload_session(conn, project_key, upload_id)
        if op.state != OP_PENDING:
            msg = f"Upload {upload_id} has state {op.state!r}, expected pending"
            raise InvalidOperationError(msg, details={"state": op.state})
        upload_staging.remove_staging_dir(project_dir, upload_id)
        mark_operation_state(conn, upload_id, OP_DISCARDED)
        return DiscardUploadResult(upload_id=upload_id, path=op.target_path)


# ── ChatGPT file-reference upload (openai/fileParams) ────────────────────────


#: Bounds worst-case per-call work: MAX_CHATGPT_FILES_PER_CALL files, each
#: up to MAX_UPLOAD_BYTES, fetched sequentially in one tool call. Aggregate
#: byte and wall-clock ceilings prevent a valid batch from multiplying the
#: per-file limit into an availability attack.


class ChatGPTFileInput(BaseModel):
    """One file reference from ChatGPT's ``openai/fileParams`` extension.

    Field shape matches ChatGPT's file-reference schema exactly (see
    product/spec-mcp.md §5.3c) — do not add, rename, or drop fields.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    download_url: str = Field(
        description="Temporary, authorized URL to fetch this file's bytes from",
        min_length=1,
        max_length=8192,
    )
    file_id: str = Field(
        description="ChatGPT's identifier for this file", min_length=1, max_length=512
    )
    mime_type: str | None = Field(
        default=None,
        description="Claimed MIME type; not trusted as authoritative",
        max_length=255,
    )
    file_name: str | None = Field(
        default=None,
        description="Suggested filename; sanitized before use",
        max_length=255,
    )


class ChatGPTFileUploadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str
    filename: str | None = None
    ok: bool
    path: str | None = None
    metadata_path: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class ChatGPTBatchUploadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[ChatGPTFileUploadResult]
    succeeded: int
    failed: int


def _normalize_mime_type(mime_type: str | None) -> str | None:
    validated = _validated_mime_type(mime_type)
    if not validated:
        return None
    normalized = validated.split(";", 1)[0].strip().lower()
    return normalized or None


def _derive_chatgpt_filename(file_id: str, file_name: str | None, mime_type: str | None) -> str:
    if file_name:
        return _validate_upload_filename(file_name)
    guessed_ext = mimetypes.guess_extension(mime_type) if mime_type else None
    return _validate_upload_filename(f"{file_id}{guessed_ext or ''}")


def upload_library_files_from_chatgpt(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    files: list[ChatGPTFileInput],
    folder_path: str = "library",
    image_policy: ImagePolicy | None = None,
) -> ChatGPTBatchUploadResult:
    """Download and store one or more ChatGPT-referenced files into library/.

    Each file's ``download_url`` is fetched directly
    (``core.remote_fetch``, SSRF-hardened, streamed, capped at
    ``MAX_UPLOAD_BYTES``) — the model never sees or reproduces the bytes,
    unlike the base64 tools. One file failing does not affect the others:
    every requested file gets its own result entry with
    ``ok``/``error_code``/``error_message``, and the response makes partial
    success explicit via ``succeeded``/``failed`` counts — this tool never
    raises for a single file's failure, only for a malformed batch (empty
    or over ``MAX_CHATGPT_FILES_PER_CALL``) or a project-level problem.
    Reuses the exact same write path as ``upload_library_file`` /
    ``finalize_library_file_upload`` (extension denylist, fail-closed
    collision, metadata sidecar, snapshot, oplog) — ``mime_type`` is stored
    as metadata only and normalized for the result, never trusted in place
    of the extension check.

    Each file's name comes from its *own* resolved reference
    (``file_name``, else ``{file_id}{guessed_ext}``), so name and bytes
    travel together in one object and cannot be crossed. This tool
    deliberately takes no caller-supplied names: matching a parallel array
    of names to resolved files would require positional identity, which the
    ``openai/fileParams`` layer does not guarantee. Use
    ``upload_library_file_from_chatgpt`` (one call per file) when the
    destination filename matters.
    """
    if not files:
        raise ValidationError("files must not be empty")
    if len(files) > MAX_CHATGPT_FILES_PER_CALL:
        raise ValidationError(
            f"Too many files in one call: {len(files)} (max {MAX_CHATGPT_FILES_PER_CALL})",
            details={"max_files": MAX_CHATGPT_FILES_PER_CALL},
        )
    folder_path = _validate_upload_folder_path(folder_path)

    results: list[ChatGPTFileUploadResult] = []
    batch_started = time.monotonic()
    downloaded_bytes = 0
    for item in files:
        normalized_mime: str | None = None
        try:
            normalized_mime = _normalize_mime_type(item.mime_type)
            filename = _derive_chatgpt_filename(item.file_id, item.file_name, normalized_mime)
            remaining_seconds = MAX_CHATGPT_BATCH_SECONDS - (time.monotonic() - batch_started)
            if remaining_seconds <= 0:
                raise DownloadTimeoutError(
                    f"Batch exceeded the {MAX_CHATGPT_BATCH_SECONDS:g}s aggregate timeout"
                )
            remaining_bytes = MAX_CHATGPT_BATCH_BYTES - downloaded_bytes
            if remaining_bytes <= 0:
                raise FileTooLargeError(
                    "Batch exhausted its aggregate download limit",
                    details={
                        "max_bytes": MAX_CHATGPT_BATCH_BYTES,
                        "scope": "batch",
                    },
                )
            file_limit = min(MAX_UPLOAD_BYTES, remaining_bytes)
            raw = fetch_remote_file(
                item.download_url,
                max_bytes=file_limit,
                total_timeout=min(DEFAULT_TOTAL_TIMEOUT, remaining_seconds),
            )
            if len(raw) > file_limit:
                raise FileTooLargeError(
                    "Download exceeded the remaining aggregate batch limit",
                    details={
                        "max_bytes": MAX_CHATGPT_BATCH_BYTES,
                        "scope": "batch",
                    },
                )
            downloaded_bytes += len(raw)
            write_result = _write_uploaded_file(
                conn,
                workspace_root,
                project_key,
                filename=filename,
                raw=raw,
                folder_path=folder_path,
                mime_type=normalized_mime,
                metadata={"chatgpt_file_id": item.file_id},
                operation_type="upload_library_files_from_chatgpt",
                image_policy=image_policy,
            )
        except FerumindError as exc:
            results.append(
                ChatGPTFileUploadResult(
                    file_id=item.file_id,
                    filename=item.file_name,
                    ok=False,
                    mime_type=normalized_mime,
                    error_code=exc.code,
                    error_message=str(exc),
                )
            )
            continue
        results.append(
            ChatGPTFileUploadResult(
                file_id=item.file_id,
                filename=filename,
                ok=True,
                path=write_result.path,
                metadata_path=write_result.metadata_path,
                mime_type=normalized_mime,
                size_bytes=write_result.size_bytes,
                sha256=write_result.sha256,
            )
        )

    succeeded = sum(1 for r in results if r.ok)
    return ChatGPTBatchUploadResult(
        results=results, succeeded=succeeded, failed=len(results) - succeeded
    )


class ChatGPTSingleUploadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str
    filename: str
    operation_id: str
    snapshot_id: str
    path: str
    metadata_path: str
    mime_type: str | None = None
    size_bytes: int
    sha256: str


def upload_library_file_from_chatgpt(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    file: ChatGPTFileInput,
    filename: str,
    folder_path: str = "library",
    image_policy: ImagePolicy | None = None,
) -> ChatGPTSingleUploadResult:
    """Download one ChatGPT-referenced file and store it under an explicitly chosen *filename*.

    This is the tool to use whenever the destination filename matters. It
    exists because **there is no stable file identity the model can name**:
    a parameter listed in ``openai/fileParams`` is populated entirely by the
    ChatGPT host at resolution time, so the model never sees ``file_id`` or
    ``download_url`` when it writes the tool call and cannot bind a chosen
    name to a specific file in a sibling argument. A batch tool taking a
    parallel array of names could therefore only ever match names to files
    *by position*, which is an undocumented property of the resolution
    layer, not a guarantee — and mismatched names on a photo set are silent,
    plausible-looking corruption, not a visible error.

    One file per call sidesteps the mapping problem entirely: the single
    resolved file and the single requested ``filename`` are unambiguously
    related, whatever the host does to argument ordering, transport URLs, or
    completion order. Upload several files by making several calls; they are
    independent and safe to issue in parallel, since each takes the project
    lock only for its own write.

    ``file.file_name`` (ChatGPT's suggested name) is deliberately ignored
    here — ``filename`` is the caller's decision — but ``file.file_id`` is
    recorded in the metadata sidecar and echoed in the result so the stored
    file remains traceable to its origin. Bytes go through exactly the same
    ingestion path as every other upload tool (``_write_uploaded_file``:
    extension denylist, fail-closed collision, sidecar, snapshot, oplog).

    Unlike the batch tool, a failure here is a plain tool error: with one
    file there is no partial success to report.
    """
    filename = _validate_upload_filename(filename)
    folder_path = _validate_upload_folder_path(folder_path)
    normalized_mime = _normalize_mime_type(file.mime_type)

    # Reject a blocked extension before spending a download on bytes that
    # `_write_uploaded_file` would refuse anyway.
    validate_path_safe(
        workspace_root,
        project_key,
        f"{folder_path}/{filename}",
        extensions_denylist=BLOCKED_UPLOAD_EXTENSIONS,
    )

    raw = fetch_remote_file(file.download_url, max_bytes=MAX_UPLOAD_BYTES)
    write_result = _write_uploaded_file(
        conn,
        workspace_root,
        project_key,
        filename=filename,
        raw=raw,
        folder_path=folder_path,
        mime_type=normalized_mime,
        metadata={"chatgpt_file_id": file.file_id},
        operation_type="upload_library_file_from_chatgpt",
        image_policy=image_policy,
    )
    return ChatGPTSingleUploadResult(
        file_id=file.file_id,
        filename=filename,
        operation_id=write_result.operation_id,
        snapshot_id=write_result.snapshot_id,
        path=write_result.path,
        metadata_path=write_result.metadata_path,
        mime_type=normalized_mime,
        size_bytes=write_result.size_bytes,
        sha256=write_result.sha256,
    )
