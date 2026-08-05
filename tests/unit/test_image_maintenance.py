"""Re-applying the storage image policy to files already on disk."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from ferumind.core.image_maintenance import compress_project_images
from ferumind.core.images import ImagePolicy
from ferumind.core.paths import WorkspaceRoot, contained_project_root
from tests.conftest import photograph_like


@pytest.fixture
def project_root(workspace: WorkspaceRoot, project: str) -> Path:
    return contained_project_root(workspace, project)


def _write_photo(root: Path, relative: str, *, width: int, height: int) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    photograph_like(width, height).save(buffer, format="JPEG", quality=95, optimize=False)
    target.write_bytes(buffer.getvalue())
    return target


def _write_sidecar(photo: Path, payload: dict[str, object]) -> Path:
    sidecar = photo.with_suffix(".json")
    sidecar.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return sidecar


class TestCompressProjectImages:
    def test_rewrites_an_oversized_photo(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        photo = _write_photo(project_root, "library/photos/a.jpg", width=3000, height=2000)
        before = photo.stat().st_size

        report = compress_project_images(
            conn, workspace, project, policy=ImagePolicy(max_edge=1024)
        )

        assert report.changed == 1
        assert report.failed == 0
        assert photo.stat().st_size < before
        with Image.open(photo) as stored:
            assert max(stored.size) == 1024

    def test_dry_run_writes_nothing(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        photo = _write_photo(project_root, "library/photos/a.jpg", width=3000, height=2000)
        digest_before = hashlib.sha256(photo.read_bytes()).hexdigest()

        report = compress_project_images(
            conn, workspace, project, policy=ImagePolicy(max_edge=1024), dry_run=True
        )

        assert report.changed == 1
        assert report.bytes_after < report.bytes_before
        assert hashlib.sha256(photo.read_bytes()).hexdigest() == digest_before
        compress_ops = conn.execute(
            "select count(*) from operations where operation_type = ?", ("compress_image",)
        ).fetchone()[0]
        assert compress_ops == 0

    def test_is_idempotent(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        _write_photo(project_root, "library/photos/a.jpg", width=3000, height=2000)
        policy = ImagePolicy(max_edge=1024)

        first = compress_project_images(conn, workspace, project, policy=policy)
        second = compress_project_images(conn, workspace, project, policy=policy)

        assert first.changed == 1
        assert second.changed == 0
        assert second.skipped == 1

    def test_updates_the_sidecar_to_match_the_stored_bytes(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        photo = _write_photo(project_root, "library/photos/a.jpg", width=3000, height=2000)
        sidecar = _write_sidecar(
            photo,
            {
                "sha256": "stale" * 12,
                "size_bytes": 1,
                "mime_type": "image/jpeg",
                "original_filename": "a.jpg",
                "caption": "agent-authored, must survive",
            },
        )

        compress_project_images(conn, workspace, project, policy=ImagePolicy(max_edge=1024))

        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        stored = photo.read_bytes()
        assert payload["sha256"] == hashlib.sha256(stored).hexdigest()
        assert payload["size_bytes"] == len(stored)
        assert payload["caption"] == "agent-authored, must survive"
        assert payload["image_compression"]["source_size_bytes"] > len(stored)

    def test_snapshots_and_logs_every_rewrite(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        photo = _write_photo(project_root, "library/photos/a.jpg", width=3000, height=2000)
        original = photo.read_bytes()

        report = compress_project_images(
            conn, workspace, project, policy=ImagePolicy(max_edge=1024)
        )
        entry = report.entries[0]

        assert entry.operation_id is not None
        assert entry.snapshot_id is not None
        row = conn.execute(
            "select operation_type, base_sha256, after_sha256 from operations where id = ?",
            (entry.operation_id,),
        ).fetchone()
        assert row[0] == "compress_image"
        assert row[1] == hashlib.sha256(original).hexdigest()
        assert row[2] == hashlib.sha256(photo.read_bytes()).hexdigest()

    def test_prior_bytes_stay_recoverable_from_the_snapshot(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        photo = _write_photo(project_root, "library/photos/a.jpg", width=3000, height=2000)
        original = photo.read_bytes()

        report = compress_project_images(
            conn, workspace, project, policy=ImagePolicy(max_edge=1024)
        )

        snapshot_dir = Path(
            conn.execute(
                "select snapshot_dir from snapshots where id = ?",
                (report.entries[0].snapshot_id,),
            ).fetchone()[0]
        )
        recovered = (snapshot_dir / "before" / "library/photos/a.jpg").read_bytes()
        assert recovered == original

    def test_never_descends_into_ferumind_internal_state(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        """Compressing snapshot copies would destroy the recovery path."""
        internal = project_root / ".ferumind" / "snapshots" / "old"
        internal.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        photograph_like(3000, 2000).save(buffer, format="JPEG", quality=95)
        preserved = buffer.getvalue()
        (internal / "a.jpg").write_bytes(preserved)

        report = compress_project_images(conn, workspace, project, policy=ImagePolicy(max_edge=512))

        assert report.scanned == 0
        assert (internal / "a.jpg").read_bytes() == preserved

    def test_a_non_image_file_is_left_alone(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        target = project_root / "library" / "notes.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"this is not a PNG despite the extension")

        report = compress_project_images(conn, workspace, project)

        assert report.changed == 0
        assert report.failed == 0
        assert target.read_bytes() == b"this is not a PNG despite the extension"
