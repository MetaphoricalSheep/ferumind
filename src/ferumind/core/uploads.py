"""Chunked-upload staging: per-file, per-chunk scratch storage on disk.

Backs ``start_library_file_upload`` / ``append_upload_chunk`` /
``finalize_library_file_upload`` (``core.upload_writes``). Chunk bytes live only in
``projects/<key>/.ferumind/uploads/<upload_id>/chunks/`` — never in the DB or
the operation log — so a large file in transit never bloats the operations
table. The pending upload session's declared identity (filename, folder,
total size/chunks, expected hash) is tracked as an ``operations`` row keyed
by ``upload_id``, reusing the same pending/applied/discarded/expired
lifecycle as patch proposals; this module only ever touches the staging
files themselves.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ferumind.core.file_io import atomic_write_bytes
from ferumind.core.paths import contained_path


def upload_staging_dir(project_dir: Path, upload_id: str) -> Path:
    return contained_path(project_dir, f".ferumind/uploads/{upload_id}")


def _chunks_dir(project_dir: Path, upload_id: str) -> Path:
    return contained_path(upload_staging_dir(project_dir, upload_id), "chunks")


def chunk_path(project_dir: Path, upload_id: str, chunk_index: int) -> Path:
    return contained_path(_chunks_dir(project_dir, upload_id), f"{chunk_index:06d}.bin")


def write_chunk(project_dir: Path, upload_id: str, chunk_index: int, data: bytes) -> None:
    """Write one chunk, atomically. Re-writing the same index replaces it."""
    target = chunk_path(project_dir, upload_id, chunk_index)
    atomic_write_bytes(target, data)


def received_chunk_indices(project_dir: Path, upload_id: str) -> set[int]:
    chunks_dir = _chunks_dir(project_dir, upload_id)
    if not chunks_dir.is_dir():
        return set()
    indices: set[int] = set()
    for entry in chunks_dir.iterdir():
        if entry.suffix == ".bin":
            try:
                indices.add(int(entry.stem))
            except ValueError:
                continue
    return indices


def staged_size_bytes(project_dir: Path, upload_id: str) -> int:
    chunks_dir = _chunks_dir(project_dir, upload_id)
    if not chunks_dir.is_dir():
        return 0
    return sum(f.stat().st_size for f in chunks_dir.glob("*.bin"))


def assemble_chunks(project_dir: Path, upload_id: str, total_chunks: int) -> bytes:
    """Concatenate chunks 0..total_chunks-1 in order. Caller must have verified completeness."""
    parts = [chunk_path(project_dir, upload_id, i).read_bytes() for i in range(total_chunks)]
    return b"".join(parts)


def remove_staging_dir(project_dir: Path, upload_id: str) -> None:
    staging = upload_staging_dir(project_dir, upload_id)
    if staging.is_dir():
        shutil.rmtree(staging)
