"""End-to-end storage behaviour: a payload costs its size once (STORE-01).

These drive the real write flows — upload, then re-compression of what was
uploaded — and assert on inodes and on the store's contents, because the
saving is invisible to every other observable: the files still exist, still
open, and still hold the same bytes.
"""

from __future__ import annotations

import base64
import hashlib
import io
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from ferumind.core import image_maintenance
from ferumind.core.blob_store import blob_path, blob_store_root
from ferumind.core.image_maintenance import compress_project_images
from ferumind.core.images import ImagePolicy
from ferumind.core.paths import WorkspaceRoot, contained_project_root
from ferumind.core.snapshots import find_snapshot_dir, read_snapshot_metadata
from ferumind.core.upload_writes import UploadResult, upload_library_file
from tests.conftest import photograph_like


@pytest.fixture
def project_root(workspace: WorkspaceRoot, project: str) -> Path:
    return contained_project_root(workspace, project)


@pytest.fixture
def store(project_root: Path) -> Path:
    return blob_store_root(project_root)


#: Small enough that the base64 body fits ``upload_library_file``'s
#: single-call cap, large enough that re-compressing it is a real change.
PHOTO_EDGE = (1000, 700)
COMPRESSED_EDGE = 512


def _photo_bytes(*, width: int = PHOTO_EDGE[0], height: int = PHOTO_EDGE[1]) -> bytes:
    buffer = io.BytesIO()
    photograph_like(width, height).save(buffer, format="JPEG", quality=75, optimize=False)
    return buffer.getvalue()


def _stored_digests(store_root: Path) -> set[str]:
    if not store_root.is_dir():
        return set()
    return {blob.name for shard in store_root.iterdir() for blob in shard.iterdir()}


def _upload(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, raw: bytes
) -> UploadResult:
    return upload_library_file(
        conn,
        workspace,
        project,
        filename="photo.jpg",
        content_base64=base64.b64encode(raw).decode("ascii"),
        image_policy=ImagePolicy(max_edge=4096),
    )


class TestUploadCostsItsSizeOnce:
    def test_the_library_file_and_its_snapshot_share_one_inode(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        project_root: Path,
        store: Path,
    ) -> None:
        raw = _photo_bytes()

        upload = _upload(conn, workspace, project, raw)

        rel = upload.path
        live = project_root / rel
        stored = live.read_bytes()
        snapshot_dir = find_snapshot_dir(project_root, upload.snapshot_id)
        assert snapshot_dir is not None
        payload = snapshot_dir / "after" / rel
        digest = hashlib.sha256(stored).hexdigest()

        assert payload.stat().st_ino == live.stat().st_ino
        assert live.stat().st_ino == blob_path(store, digest).stat().st_ino
        assert live.stat().st_nlink == 3
        assert _stored_digests(store) == {digest}

    def test_the_uploaded_file_keeps_its_private_mode(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        project_root: Path,
    ) -> None:
        rel = _upload(conn, workspace, project, _photo_bytes()).path

        assert (project_root / rel).stat().st_mode & 0o777 == 0o600


class TestCompressionStaysReversible:
    def test_it_yields_two_blobs_and_the_original_survives_intact(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        project_root: Path,
        store: Path,
    ) -> None:
        rel = _upload(conn, workspace, project, _photo_bytes()).path
        live = project_root / rel
        original = live.read_bytes()
        original_digest = hashlib.sha256(original).hexdigest()

        report = compress_project_images(
            conn, workspace, project, policy=ImagePolicy(max_edge=COMPRESSED_EDGE)
        )

        assert report.changed == 1
        compressed = live.read_bytes()
        compressed_digest = hashlib.sha256(compressed).hexdigest()
        assert compressed != original
        assert _stored_digests(store) == {original_digest, compressed_digest}

        # The whole risk of sharing an inode with the live file: rewriting it
        # must not rewrite the history it was snapshotted into.
        entry_snapshot_id = report.entries[0].snapshot_id
        assert entry_snapshot_id is not None
        snapshot_dir = find_snapshot_dir(project_root, entry_snapshot_id)
        assert snapshot_dir is not None
        assert (snapshot_dir / "before" / rel).read_bytes() == original
        assert (snapshot_dir / "after" / rel).read_bytes() == compressed
        assert live.stat().st_ino == blob_path(store, compressed_digest).stat().st_ino

    def test_the_recorded_digests_still_describe_the_stored_bytes(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        project_root: Path,
    ) -> None:
        """Restore integrity is a digest check; it must pass after linking."""
        rel = _upload(conn, workspace, project, _photo_bytes()).path
        report = compress_project_images(
            conn, workspace, project, policy=ImagePolicy(max_edge=COMPRESSED_EDGE)
        )

        entry_snapshot_id = report.entries[0].snapshot_id
        assert entry_snapshot_id is not None
        snapshot_dir = find_snapshot_dir(project_root, entry_snapshot_id)
        assert snapshot_dir is not None
        metadata = read_snapshot_metadata(snapshot_dir)
        assert metadata is not None
        before = (snapshot_dir / "before" / rel).read_bytes()
        after = (snapshot_dir / "after" / rel).read_bytes()
        assert hashlib.sha256(before).hexdigest() == metadata.before_sha256
        assert hashlib.sha256(after).hexdigest() == metadata.after_sha256
        assert metadata.before_size_bytes == len(before)
        assert metadata.after_size_bytes == len(after)

    def test_a_failure_after_publishing_returns_the_original_bytes(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        project_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The rollback path, with the live file linked to the new blob.

        Breaking that link is exactly what returning the original requires,
        so this is the case where a plain rewrite beats a re-link.
        """
        rel = _upload(conn, workspace, project, _photo_bytes()).path
        live = project_root / rel
        original = live.read_bytes()

        def fail_after_publish(*args: object, **kwargs: object) -> str:
            raise OSError("injected bookkeeping failure")

        monkeypatch.setattr(image_maintenance, "record_operation", fail_after_publish)

        with pytest.raises(OSError, match="injected bookkeeping failure"):
            compress_project_images(
                conn, workspace, project, policy=ImagePolicy(max_edge=COMPRESSED_EDGE)
            )

        assert live.read_bytes() == original
        assert live.stat().st_mode & 0o777 == 0o600


def test_an_image_written_outside_ferumind_keeps_its_own_mode(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    project_root: Path,
) -> None:
    """An operator-placed file is group-readable; linking must not change that.

    A hardlink cannot carry a mode of its own, so this file is deliberately
    *not* deduplicated — ``file_io``'s mode guarantee outranks the saving.
    """
    photo = project_root / "library" / "operator.jpg"
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(_photo_bytes())
    photo.chmod(0o644)

    report = compress_project_images(
        conn, workspace, project, policy=ImagePolicy(max_edge=COMPRESSED_EDGE)
    )

    assert report.changed == 1
    assert photo.stat().st_mode & 0o777 == 0o644
    with Image.open(photo) as stored:
        assert max(stored.size) == COMPRESSED_EDGE
