"""Content-addressed payload storage shared through hardlinks.

A snapshot's payload and the live file it snapshots are byte-identical, and
so is the ``before`` of one edit and the ``after`` of the one before it.
Written as separate copies, a workspace pays for every version of every file
once per name that mentions it. This module writes the bytes once, under
their own SHA-256, and gives every name a hardlink to that one inode.

**Snapshot payloads stay real files at their existing paths.** A blob is not
an indirection a reader has to know about:
``.ferumind/snapshots/<ts>-<id>/after/library/x.jpg`` still exists, still
opens, and still reads its own bytes — it merely shares an inode with
``library/x.jpg`` and with the blob. Nothing on the read, restore, or MCP
side changes, and nothing here is a new format.

**The reference count is the kernel's.** ``st_nlink`` is the only bookkeeping:
deleting a snapshot directory is still ``rm -rf``, and the bytes survive for
as long as some other name holds them. :func:`sweep_unreferenced` is
therefore "unlink what nothing points at", which is crash-safe by
construction and needs no database.

**Hardlinking is best-effort, never a guarantee.** Network mounts, exFAT, and
hardened mount options all refuse ``link``. Every operation here falls back
to a byte copy in that case and reports it through ``BlobRef.linked``; the
promise is "stored once where the filesystem allows it", and a workspace that
cannot link behaves exactly as it did before this module existed.

One consequence is worth stating plainly: because a snapshot payload shares
an inode with the live file, a tool that rewrites that file *in place* also
rewrites the snapshot's bytes. Ferumind's own writes cannot do this — every
one of them renames a fresh inode over the name, which breaks the link
correctly — and a third-party in-place write is caught by the digest check in
``read_snapshot_before_content``, which fails closed rather than restoring
wrong bytes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ferumind.core.file_io import (
    atomic_write_bytes,
    ensure_private_directory,
    existing_regular_file_mode,
    read_regular_file_bytes,
    sha256_regular_file,
)
from ferumind.core.paths import PathSafetyError, contained_path

#: Where a store lives relative to a project root or the workspace root.
BLOB_STORE_RELATIVE: Final = ".ferumind/blobs"

_DIGEST_PATTERN: Final = re.compile(r"\A[0-9a-f]{64}\Z")

#: Filesystems that refuse ``link`` say so in one of these ways: a different
#: device (``EXDEV``), a mount or security policy that forbids it (``EPERM``),
#: an inode already at its link limit (``EMLINK``), or a filesystem without
#: the concept at all (``ENOTSUP``). None of them is a Ferumind bug, and none
#: of them may fail a write — they degrade to a copy.
_FALLBACK_ERRNOS: Final[frozenset[int]] = frozenset(
    {errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP, errno.EOPNOTSUPP}
)


class BlobIntegrityError(ValueError):
    """Raised when a blob's stored bytes contradict the digest naming them.

    Local to this module rather than one of ``core.errors``' coded types on
    purpose: it is a storage invariant, not a condition any caller can
    correct, and there is no MCP error code that would tell an agent anything
    useful. It reaches the tool boundary as an internal error, which is what
    a corrupted store is. ``ValueError`` keeps it inside the ``except
    ValueError`` handling every other core error already lands in.
    """


class BlobMissingError(ValueError):
    """Raised when a :class:`BlobRef` names a blob that is no longer stored."""


@dataclass(frozen=True)
class BlobRef:
    """One stored payload: its digest, its length, and how it got there."""

    digest: str
    size_bytes: int
    #: ``False`` when the filesystem refused to link and the bytes were
    #: copied instead. The operation succeeded either way; only the space
    #: saving is lost.
    linked: bool


@dataclass(frozen=True)
class SweepResult:
    """What one :func:`sweep_unreferenced` pass removed and what it kept."""

    removed: int
    kept: int
    bytes_reclaimed: int


def blob_store_root(base: Path) -> Path:
    """Return the store for *base*, a project root or the workspace root."""
    return contained_path(base, BLOB_STORE_RELATIVE)


def blob_path(store_root: Path, digest: str) -> Path:
    """Resolve *digest*'s path in *store_root*, validating before joining.

    The digest is the only caller-influenced component in this module, so it
    is checked against its exact shape — 64 lowercase hex characters —
    *before* it is ever concatenated into a path, and the result is resolved
    through :func:`contained_path` afterwards. Traversal, absolute paths, and
    a symlink swapped in at any component are all refused there.
    """
    if not _DIGEST_PATTERN.match(digest):
        msg = "A blob digest must be 64 lowercase hexadecimal characters"
        raise PathSafetyError(msg)
    return contained_path(store_root, f"{digest[:2]}/{digest}")


def store_bytes(store_root: Path, data: bytes) -> BlobRef:
    """Store *data* under its digest, returning a reference to it.

    An already-stored blob is checked by *size* rather than re-hashed. The
    write path runs on every snapshot, and re-reading a stored payload to
    confirm what its name already asserts would trade the whole point of the
    store for a guarantee the digest check on restore already provides. A
    size mismatch is corruption and raises; it is never a silent reuse. Full
    re-verification belongs to conversion and to ``verify-index``.
    """
    digest = hashlib.sha256(data).hexdigest()
    blob = blob_path(store_root, digest)
    stored_size = _stored_size(blob)
    if stored_size is not None:
        _assert_size(blob, stored_size, len(data))
        return BlobRef(digest=digest, size_bytes=len(data), linked=True)

    _private_directory(blob.parent)
    directory_fd = os.open(blob.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        linked = _publish_bytes(data, blob.name, directory_fd)
    finally:
        os.close(directory_fd)
    return BlobRef(digest=digest, size_bytes=len(data), linked=linked)


def adopt_file(store_root: Path, source: Path) -> BlobRef:
    """Make *source* and its blob one inode, without copying its bytes.

    When the blob is absent, *source* itself becomes the blob: one ``link``
    call, no read of the payload beyond the digest. When it is already
    stored, *source* is replaced by a link to it, which is where the space is
    actually reclaimed.
    """
    digest, size_bytes = sha256_regular_file(source)
    blob = blob_path(store_root, digest)
    stored_size = _stored_size(blob)
    if stored_size is not None:
        _assert_size(blob, stored_size, size_bytes)
        ref = BlobRef(digest=digest, size_bytes=size_bytes, linked=True)
        link_into(store_root, ref, source)
        return ref

    _private_directory(blob.parent)
    directory_fd = os.open(blob.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            os.link(source, blob.name, dst_dir_fd=directory_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            if exc.errno not in _FALLBACK_ERRNOS:
                raise
            copied = store_bytes(store_root, read_regular_file_bytes(source))
            return dataclasses.replace(copied, linked=False)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return BlobRef(digest=digest, size_bytes=size_bytes, linked=True)


def link_into(store_root: Path, ref: BlobRef, target: Path) -> None:
    """Publish *ref*'s bytes at *target*, sharing the blob's inode.

    Always link-then-rename, never unlink-then-link: a crash mid-operation
    must leave either the old file or the new one at that name, and never
    neither.

    A hardlink shares the inode, so it cannot carry a mode of its own. When
    *target* already exists with a mode the blob does not have, linking would
    silently rewrite an operator's chosen permissions — ``file_io``'s standing
    guarantee is that an existing file's mode survives being rewritten — so
    that case falls back to a byte copy and keeps the mode. New files are
    created at the blob's ``0600``, matching ``_atomic_write``.
    """
    blob = blob_path(store_root, ref.digest)
    try:
        blob_status = os.stat(blob, follow_symlinks=False)
    except FileNotFoundError as exc:
        msg = f"Blob {ref.digest} is not in the store"
        raise BlobMissingError(msg) from exc
    if not stat.S_ISREG(blob_status.st_mode):
        msg = f"Blob {ref.digest} is not a regular file"
        raise PathSafetyError(msg)

    # ``ensure_private_directory`` and not ``_private_directory``: a target is
    # very often a live ``library/`` path, an operator-owned directory whose
    # mode Ferumind creates and then leaves alone (S-09). Only the store's own
    # directories get their mode re-asserted.
    ensure_private_directory(target.parent)
    directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        existing_mode = existing_regular_file_mode(target.name, directory_fd)
        if existing_mode is not None and existing_mode != stat.S_IMODE(blob_status.st_mode):
            _copy_blob(blob, target)
            return
        _link_over(blob, target, directory_fd)
    finally:
        os.close(directory_fd)


def sweep_unreferenced(store_root: Path) -> SweepResult:
    """Unlink every blob no other name holds, and report what that freed.

    ``st_nlink == 1`` means the store itself is the only reference left, so
    the payload is unreachable from any snapshot or live file and the space
    is safe to reclaim. Anything with a second link is untouched. Entries
    that are not plainly a blob — a stray temporary file, a symlink swapped
    into the store, a subdirectory — are left exactly where they are rather
    than guessed about.
    """
    removed = 0
    kept = 0
    bytes_reclaimed = 0
    for blob in _stored_blobs(store_root):
        try:
            status = os.stat(blob, follow_symlinks=False)
            if not stat.S_ISREG(status.st_mode):
                continue
            if status.st_nlink > 1:
                kept += 1
                continue
            os.unlink(blob)
            removed += 1
            bytes_reclaimed += status.st_size
        except OSError:
            continue
    return SweepResult(removed=removed, kept=kept, bytes_reclaimed=bytes_reclaimed)


def _stored_blobs(store_root: Path) -> list[Path]:
    """Return every validly named blob in *store_root*, and nothing else.

    Each candidate is re-resolved through :func:`blob_path`, so a name that
    is not a digest, a shard that does not match its contents, and a symlink
    at any component are all dropped here rather than acted on.
    """
    if not store_root.is_dir():
        return []
    # Walk the resolved root so each entry is directly comparable with what
    # ``blob_path`` returns, which is always resolved.
    found: list[Path] = []
    for shard in sorted(store_root.resolve().iterdir()):
        if shard.is_symlink() or not shard.is_dir():
            continue
        for entry in sorted(shard.iterdir()):
            try:
                blob = blob_path(store_root, entry.name)
            except PathSafetyError:
                continue
            if blob == entry:
                found.append(blob)
    return found


def _private_directory(path: Path) -> None:
    """Create *path* at ``0700`` and re-assert that mode on every touch.

    Blobs are verbatim document and upload payloads, so a store is exactly as
    sensitive as the snapshot tree beside it. ``.ferumind/`` is Ferumind's own
    state rather than a directory the operator arranges, which is why the mode
    is forced here instead of merely set on create. See ``core.file_io``.

    Every missing ancestor is created through ``ensure_private_directory``
    rather than ``mkdir(parents=True)``, which would apply the mode to the
    shard alone and leave ``.ferumind/blobs`` itself at the process umask.
    """
    ensure_private_directory(path)
    path.chmod(0o700)


def _stored_size(blob: Path) -> int | None:
    """Return the stored blob's size, or ``None`` when it is not stored."""
    try:
        status = os.stat(blob, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not stat.S_ISREG(status.st_mode):
        msg = f"Blob path {blob.name} is not a regular file"
        raise PathSafetyError(msg)
    return status.st_size


def _assert_size(blob: Path, stored_size: int, expected_size: int) -> None:
    if stored_size == expected_size:
        return
    msg = f"Blob {blob.name} holds {stored_size} bytes where its digest describes {expected_size}"
    raise BlobIntegrityError(msg)


def _publish_bytes(data: bytes, blob_name: str, directory_fd: int) -> bool:
    """Write *data* to a private temporary name and publish it as *blob_name*.

    Returns whether the publish was a link. ``FileExistsError`` on that link
    is success rather than a collision: another writer stored the same bytes
    under the same digest first, which is the whole point of addressing
    content by its hash.
    """
    temporary_name = f".ferumind_blob_{secrets.token_hex(8)}"
    fd = os.open(
        temporary_name,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC,
        0o600,
        dir_fd=directory_fd,
    )
    published = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        linked = True
        try:
            os.link(
                temporary_name,
                blob_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        except FileExistsError:
            pass
        except OSError as exc:
            if exc.errno not in _FALLBACK_ERRNOS:
                raise
            os.replace(
                temporary_name,
                blob_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            published = True
            linked = False
        os.fsync(directory_fd)
    finally:
        if not published:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)
    return linked


def _link_over(blob: Path, target: Path, directory_fd: int) -> None:
    """Link *blob* to a temporary name beside *target* and rename it over."""
    temporary_name = f".ferumind_blob_{secrets.token_hex(8)}"
    try:
        os.link(blob, temporary_name, dst_dir_fd=directory_fd)
    except OSError as exc:
        if exc.errno not in _FALLBACK_ERRNOS:
            raise
        _copy_blob(blob, target)
        return
    try:
        os.replace(
            temporary_name,
            target.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        raise


def _copy_blob(blob: Path, target: Path) -> None:
    """Publish the blob's bytes at *target* as an ordinary independent file."""
    atomic_write_bytes(target, read_regular_file_bytes(blob))
