"""Tests for upload_library_file: binary uploads pinned under library/."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from lattice.core import uploads as upload_staging
from lattice.core import writes
from lattice.core.errors import (
    ContentHashMismatchError,
    DocumentExistsError,
    FileTooLargeError,
    InvalidOperationError,
    OperationNotFoundError,
    PatchExpiredError,
    PatchProjectMismatchError,
    UnknownFolderError,
    UnsupportedFileTypeError,
    UploadIncompleteError,
    ValidationError,
    WorkspaceMismatchError,
)
from lattice.core.images import ImagePolicy
from lattice.core.operations import get_operation, list_operations
from lattice.core.paths import WorkspaceRoot
from lattice.core.reads import read_project_snapshot
from lattice.core.snapshots import list_snapshots_from_db
from lattice.core.writes import (
    append_upload_chunk,
    discard_upload,
    finalize_library_file_upload,
    start_library_file_upload,
    upload_library_file,
)
from tests.conftest import photograph_like

_PDF_BYTES = b"%PDF-1.4 fake but good enough for a test\n\xff"
_PDF_B64 = base64.b64encode(_PDF_BYTES).decode("ascii")


def _read_bytes(workspace: WorkspaceRoot, project: str, rel: str) -> bytes:
    return (workspace / "projects" / project / rel).read_bytes()


class TestUploadImageNormalization:
    """Rasters are normalized on the way in; the original is not retained."""

    def _jpeg(self, width: int, height: int) -> bytes:
        # Kept under MAX_CHUNK_BYTES: the single-call base64 cap is a wire-size
        # limit checked before compression, so an oversized fixture would fail
        # for an unrelated reason.
        buffer = io.BytesIO()
        photograph_like(width, height).save(buffer, format="JPEG", quality=80, optimize=False)
        return buffer.getvalue()

    def test_image_is_downscaled_before_it_is_stored(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        source = self._jpeg(1200, 900)

        result = upload_library_file(
            conn,
            workspace,
            project,
            filename="photo.jpg",
            content_base64=base64.b64encode(source).decode("ascii"),
            image_policy=ImagePolicy(max_edge=512, jpeg_quality=85),
        )

        stored = _read_bytes(workspace, project, result.path)
        assert len(stored) < len(source)
        with Image.open(io.BytesIO(stored)) as image:
            assert max(image.size) == 512

    def test_recorded_hash_and_size_describe_the_stored_bytes(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        source = self._jpeg(1200, 900)

        result = upload_library_file(
            conn,
            workspace,
            project,
            filename="photo.jpg",
            content_base64=base64.b64encode(source).decode("ascii"),
            image_policy=ImagePolicy(max_edge=512),
        )

        stored = _read_bytes(workspace, project, result.path)
        sidecar = json.loads(
            (workspace / "projects" / project / result.metadata_path).read_text(encoding="utf-8")
        )
        assert result.sha256 == hashlib.sha256(stored).hexdigest()
        assert result.size_bytes == len(stored)
        assert sidecar["sha256"] == hashlib.sha256(stored).hexdigest()
        assert sidecar["size_bytes"] == len(stored)
        # Provenance: the caller's original size is still recoverable.
        assert sidecar["image_compression"]["source_size_bytes"] == len(source)

    def test_non_image_upload_is_stored_byte_for_byte(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        result = upload_library_file(
            conn,
            workspace,
            project,
            filename="report.pdf",
            content_base64=_PDF_B64,
            image_policy=ImagePolicy(max_edge=512),
        )

        assert _read_bytes(workspace, project, result.path) == _PDF_BYTES

    def test_disabled_policy_stores_the_original_image(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        source = self._jpeg(1200, 900)

        result = upload_library_file(
            conn,
            workspace,
            project,
            filename="photo.jpg",
            content_base64=base64.b64encode(source).decode("ascii"),
            image_policy=ImagePolicy(enabled=False),
        )

        assert _read_bytes(workspace, project, result.path) == source


class TestUploadLibraryFile:
    def test_uploads_into_library_with_metadata_sidecar(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        result = upload_library_file(
            conn,
            workspace,
            project,
            filename="report.pdf",
            content_base64=_PDF_B64,
            mime_type="application/pdf",
            metadata={"source": "chatgpt-upload", "tags": ["quarterly"]},
        )

        assert result.path == "library/report.pdf"
        assert result.metadata_path == "library/report.json"
        assert result.folder == "library"
        assert result.size_bytes == len(_PDF_BYTES)
        assert result.sha256 == hashlib.sha256(_PDF_BYTES).hexdigest()

        assert _read_bytes(workspace, project, result.path) == _PDF_BYTES

        sidecar = json.loads(_read_bytes(workspace, project, result.metadata_path))
        assert sidecar["source"] == "chatgpt-upload"
        assert sidecar["tags"] == ["quarterly"]
        assert sidecar["sha256"] == result.sha256
        assert sidecar["size_bytes"] == len(_PDF_BYTES)
        assert sidecar["mime_type"] == "application/pdf"
        assert sidecar["original_filename"] == "report.pdf"
        assert sidecar["uploaded_by_tool"] == "upload_library_file"
        assert "uploaded_at" in sidecar

        op = get_operation(conn, result.operation_id)
        assert op is not None
        assert op.operation_type == "upload_library_file"
        assert op.target_path == result.path
        assert op.after_sha256 == result.sha256

        snapshots = list_snapshots_from_db(conn, project_key=project, target_path=result.path)
        assert len(snapshots) == 1
        assert snapshots[0].id == result.snapshot_id

        snapshot = read_project_snapshot(workspace, project, snapshots[0].id)
        assert snapshot.before_content is None
        assert snapshot.before_content_omitted is False
        assert snapshot.after_content is None
        assert snapshot.after_content_omitted is True
        assert "Binary file added" in snapshot.diff

    def test_agent_cannot_override_protected_metadata_fields(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        result = upload_library_file(
            conn,
            workspace,
            project,
            filename="spoof.pdf",
            content_base64=_PDF_B64,
            metadata={"sha256": "not-the-real-hash", "size_bytes": -1},
        )
        sidecar = json.loads(_read_bytes(workspace, project, result.metadata_path))
        assert sidecar["sha256"] == result.sha256
        assert sidecar["size_bytes"] == len(_PDF_BYTES)

    def test_json_upload_uses_distinct_metadata_sidecar(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        payload = base64.b64encode(b'{"real": "content"}').decode("ascii")
        result = upload_library_file(
            conn,
            workspace,
            project,
            filename="dataset.json",
            content_base64=payload,
        )
        assert result.path == "library/dataset.json"
        assert result.metadata_path == "library/dataset.json.metadata.json"
        assert _read_bytes(workspace, project, result.path) == b'{"real": "content"}'
        assert json.loads(_read_bytes(workspace, project, result.metadata_path))["sha256"]

    def test_nested_folder_under_library(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        result = upload_library_file(
            conn,
            workspace,
            project,
            filename="receipt.pdf",
            content_base64=_PDF_B64,
            folder_path="library/attachments/receipts",
        )
        assert result.path == "library/attachments/receipts/receipt.pdf"
        assert result.folder == "library"

    def test_folder_path_outside_library_rejected(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(UnknownFolderError):
            upload_library_file(
                conn,
                workspace,
                project,
                filename="report.pdf",
                content_base64=_PDF_B64,
                folder_path="canvases",
            )

    def test_collision_fails_closed(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        upload_library_file(
            conn, workspace, project, filename="report.pdf", content_base64=_PDF_B64
        )
        with pytest.raises(DocumentExistsError):
            upload_library_file(
                conn, workspace, project, filename="report.pdf", content_base64=_PDF_B64
            )

    @pytest.mark.parametrize("failure_point", ["snapshot_row", "operation_row"])
    def test_durable_bookkeeping_failure_rolls_back_uploaded_files_and_snapshot(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
        failure_point: str,
    ) -> None:
        snapshots_root = workspace / "projects" / project / ".lattice" / "snapshots"
        before_dirs: set[Path] = (
            {Path(entry) for entry in snapshots_root.iterdir()}
            if snapshots_root.is_dir()
            else set()
        )
        before_snapshot_rows = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        before_operation_rows = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]

        def fail(*_args: object, **_kwargs: object) -> str:
            raise sqlite3.OperationalError("synthetic durable bookkeeping failure")

        if failure_point == "snapshot_row":
            monkeypatch.setattr(writes, "record_snapshot_in_db", fail)
        else:
            monkeypatch.setattr(writes, "record_operation", fail)

        with pytest.raises(sqlite3.OperationalError, match="synthetic"):
            upload_library_file(
                conn,
                workspace,
                project,
                filename="rollback.pdf",
                content_base64=_PDF_B64,
            )

        assert not (workspace / "projects" / project / "library/rollback.pdf").exists()
        assert not (workspace / "projects" / project / "library/rollback.json").exists()
        after_dirs: set[Path] = (
            {Path(entry) for entry in snapshots_root.iterdir()}
            if snapshots_root.is_dir()
            else set()
        )
        assert after_dirs == before_dirs
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == before_snapshot_rows
        assert (
            conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == before_operation_rows
        )

    @pytest.mark.parametrize("filename", ["payload.sh", "notes.md"])
    def test_blocked_extension_rejected(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        filename: str,
    ) -> None:
        with pytest.raises(UnsupportedFileTypeError):
            upload_library_file(
                conn, workspace, project, filename=filename, content_base64=_PDF_B64
            )

    def test_oversized_payload_rejected(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Exercise the boundary against a small patched cap rather than
        # allocating/base64-encoding a real MAX_CHUNK_BYTES-sized payload
        # (upload_library_file is capped at MAX_CHUNK_BYTES since its whole
        # call has to fit in one tool call, same as a single chunk).
        monkeypatch.setattr(writes, "MAX_CHUNK_BYTES", 16)
        big = base64.b64encode(b"x" * 17).decode("ascii")
        with pytest.raises(FileTooLargeError):
            upload_library_file(conn, workspace, project, filename="big.bin", content_base64=big)

    def test_invalid_base64_rejected(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(ValidationError):
            upload_library_file(
                conn,
                workspace,
                project,
                filename="report.pdf",
                content_base64="not-valid-base64!!!",
            )

    def test_filename_must_be_bare(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        for bad_name in (
            "../escape.pdf",
            "sub/dir.pdf",
            "",
            "   ",
            ".hidden",
            "bad\nname.pdf",
            "report.pdf\n",
            " report.pdf",
        ):
            with pytest.raises(ValidationError):
                upload_library_file(
                    conn, workspace, project, filename=bad_name, content_base64=_PDF_B64
                )

    def test_hidden_nested_folder_is_rejected(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(WorkspaceMismatchError, match="Hidden paths"):
            upload_library_file(
                conn,
                workspace,
                project,
                filename="report.pdf",
                content_base64=_PDF_B64,
                folder_path="library/.hidden",
            )

    @pytest.mark.parametrize(
        "folder_path",
        (
            "/library",
            "library/",
            "library//nested",
            r"library\nested",
            "library/../canvases",
            " library",
            "library\n",
        ),
    )
    def test_noncanonical_folder_path_is_rejected(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        folder_path: str,
    ) -> None:
        with pytest.raises(ValidationError):
            upload_library_file(
                conn,
                workspace,
                project,
                filename="report.pdf",
                content_base64=_PDF_B64,
                folder_path=folder_path,
            )

    def test_oversized_metadata_is_rejected_before_write(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(writes, "MAX_UPLOAD_METADATA_BYTES", 8)
        with pytest.raises(FileTooLargeError):
            upload_library_file(
                conn,
                workspace,
                project,
                filename="report.pdf",
                content_base64=_PDF_B64,
                metadata={"long": "value"},
            )

    def test_invalid_mime_type_is_rejected(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(ValidationError):
            upload_library_file(
                conn,
                workspace,
                project,
                filename="report.pdf",
                content_base64=_PDF_B64,
                mime_type="application/pdf\nInjected: value",
            )


def _b64_chunks(data: bytes, n: int) -> list[str]:
    size = -(-len(data) // n)  # ceil div
    pieces = [data[i : i + size] for i in range(0, len(data), size)] or [b""]
    return [base64.b64encode(p).decode("ascii") for p in pieces]


class TestChunkedUpload:
    def test_start_rejects_markdown_binary_upload(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(UnsupportedFileTypeError):
            start_library_file_upload(
                conn,
                workspace,
                project,
                filename="notes.md",
                total_size=1,
                total_chunks=1,
            )

    def test_start_rejects_malformed_expected_hash(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(ValidationError, match="64 hexadecimal"):
            start_library_file_upload(
                conn,
                workspace,
                project,
                filename="x.bin",
                total_size=1,
                total_chunks=1,
                expected_sha256="not-a-hash",
            )

    def test_start_rejects_excessive_or_impossible_chunk_counts(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(writes, "MAX_UPLOAD_CHUNKS", 3)
        with pytest.raises(ValidationError, match="maximum"):
            start_library_file_upload(
                conn, workspace, project, filename="x.bin", total_size=1, total_chunks=4
            )
        monkeypatch.setattr(writes, "MAX_CHUNK_BYTES", 4)
        with pytest.raises(ValidationError, match="cannot fit"):
            start_library_file_upload(
                conn, workspace, project, filename="x.bin", total_size=5, total_chunks=1
            )

    def test_happy_path_matches_one_shot_result_shape(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        chunks = _b64_chunks(_PDF_BYTES, 3)
        session = start_library_file_upload(
            conn,
            workspace,
            project,
            filename="chunked.pdf",
            total_size=len(_PDF_BYTES),
            total_chunks=len(chunks),
            mime_type="application/pdf",
            metadata={"source": "chunked-upload"},
            expected_sha256=hashlib.sha256(_PDF_BYTES).hexdigest(),
        )
        assert session.upload_id
        assert session.expires_at

        last_progress = None
        for index, chunk in enumerate(chunks):
            last_progress = append_upload_chunk(
                conn,
                workspace,
                project,
                upload_id=session.upload_id,
                chunk_index=index,
                chunk_base64=chunk,
            )
        assert last_progress is not None
        assert last_progress.received_chunks == len(chunks)
        assert last_progress.total_chunks == len(chunks)
        assert last_progress.received_bytes == len(_PDF_BYTES)

        result = finalize_library_file_upload(conn, workspace, project, upload_id=session.upload_id)
        assert result.path == "library/chunked.pdf"
        assert result.sha256 == hashlib.sha256(_PDF_BYTES).hexdigest()
        assert _read_bytes(workspace, project, result.path) == _PDF_BYTES

        sidecar = json.loads(_read_bytes(workspace, project, result.metadata_path))
        assert sidecar["source"] == "chunked-upload"
        assert sidecar["uploaded_by_tool"] == "finalize_library_file_upload"

        op = get_operation(conn, result.operation_id)
        assert op is not None
        assert op.operation_type == "finalize_library_file_upload"

        # Staging area is cleaned up after finalize.
        staging = workspace / "projects" / project / ".lattice" / "uploads" / session.upload_id
        assert not staging.exists()

    def test_chunk_resend_is_idempotent(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        chunks = _b64_chunks(_PDF_BYTES, 2)
        session = start_library_file_upload(
            conn,
            workspace,
            project,
            filename="resend.pdf",
            total_size=len(_PDF_BYTES),
            total_chunks=len(chunks),
        )
        for index, chunk in enumerate(chunks):
            append_upload_chunk(
                conn,
                workspace,
                project,
                upload_id=session.upload_id,
                chunk_index=index,
                chunk_base64=chunk,
            )
        # Resend chunk 0 — should overwrite, not duplicate.
        progress = append_upload_chunk(
            conn,
            workspace,
            project,
            upload_id=session.upload_id,
            chunk_index=0,
            chunk_base64=chunks[0],
        )
        assert progress.received_chunks == len(chunks)
        assert progress.received_bytes == len(_PDF_BYTES)

        result = finalize_library_file_upload(conn, workspace, project, upload_id=session.upload_id)
        assert _read_bytes(workspace, project, result.path) == _PDF_BYTES

    def test_finalize_rejects_missing_chunks(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        chunks = _b64_chunks(_PDF_BYTES, 3)
        session = start_library_file_upload(
            conn,
            workspace,
            project,
            filename="incomplete.pdf",
            total_size=len(_PDF_BYTES),
            total_chunks=len(chunks),
        )
        append_upload_chunk(
            conn,
            workspace,
            project,
            upload_id=session.upload_id,
            chunk_index=0,
            chunk_base64=chunks[0],
        )
        with pytest.raises(UploadIncompleteError):
            finalize_library_file_upload(conn, workspace, project, upload_id=session.upload_id)

    def test_finalize_rejects_hash_mismatch(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        chunks = _b64_chunks(_PDF_BYTES, 1)
        session = start_library_file_upload(
            conn,
            workspace,
            project,
            filename="tampered.pdf",
            total_size=len(_PDF_BYTES),
            total_chunks=1,
            expected_sha256="0" * 64,
        )
        append_upload_chunk(
            conn,
            workspace,
            project,
            upload_id=session.upload_id,
            chunk_index=0,
            chunk_base64=chunks[0],
        )
        with pytest.raises(ContentHashMismatchError):
            finalize_library_file_upload(conn, workspace, project, upload_id=session.upload_id)

    def test_finalize_terminal_state_failure_rolls_back_published_content(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = start_library_file_upload(
            conn,
            workspace,
            project,
            filename="atomic.pdf",
            total_size=len(_PDF_BYTES),
            total_chunks=1,
        )
        append_upload_chunk(
            conn,
            workspace,
            project,
            upload_id=session.upload_id,
            chunk_index=0,
            chunk_base64=_PDF_B64,
        )

        def fail_terminal_state(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("injected terminal-state failure")

        monkeypatch.setattr(writes, "mark_operation_state", fail_terminal_state)
        with pytest.raises(sqlite3.OperationalError, match="terminal-state"):
            finalize_library_file_upload(
                conn,
                workspace,
                project,
                upload_id=session.upload_id,
            )

        session_operation = get_operation(conn, session.upload_id)
        assert session_operation is not None
        assert session_operation.state == "pending"
        assert not (workspace / "projects" / project / "library/atomic.pdf").exists()
        assert not (workspace / "projects" / project / "library/atomic.json").exists()
        assert not any(
            operation.operation_type == "finalize_library_file_upload"
            for operation in list_operations(conn, project)
        )

    def test_chunk_index_out_of_range_rejected(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        session = start_library_file_upload(
            conn, workspace, project, filename="x.pdf", total_size=10, total_chunks=2
        )
        with pytest.raises(ValidationError):
            append_upload_chunk(
                conn,
                workspace,
                project,
                upload_id=session.upload_id,
                chunk_index=2,
                chunk_base64=_PDF_B64,
            )

    def test_chunk_over_per_chunk_cap_rejected(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(writes, "MAX_CHUNK_BYTES", 4)
        session = start_library_file_upload(
            conn, workspace, project, filename="x.pdf", total_size=4, total_chunks=1
        )
        big_chunk = base64.b64encode(b"x" * 5).decode("ascii")
        with pytest.raises(FileTooLargeError):
            append_upload_chunk(
                conn,
                workspace,
                project,
                upload_id=session.upload_id,
                chunk_index=0,
                chunk_base64=big_chunk,
            )

    def test_cumulative_size_over_declared_total_rejected(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        session = start_library_file_upload(
            conn, workspace, project, filename="x.pdf", total_size=4, total_chunks=2
        )
        append_upload_chunk(
            conn,
            workspace,
            project,
            upload_id=session.upload_id,
            chunk_index=0,
            chunk_base64=base64.b64encode(b"xxxx").decode("ascii"),
        )
        with pytest.raises(FileTooLargeError):
            append_upload_chunk(
                conn,
                workspace,
                project,
                upload_id=session.upload_id,
                chunk_index=1,
                chunk_base64=base64.b64encode(b"xxxx").decode("ascii"),
            )
        # Session is now failed; further appends are rejected.
        with pytest.raises(InvalidOperationError):
            append_upload_chunk(
                conn,
                workspace,
                project,
                upload_id=session.upload_id,
                chunk_index=0,
                chunk_base64=base64.b64encode(b"x").decode("ascii"),
            )

    def test_start_rejects_folder_outside_library(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(UnknownFolderError):
            start_library_file_upload(
                conn,
                workspace,
                project,
                filename="x.pdf",
                total_size=10,
                total_chunks=1,
                folder_path="canvases",
            )

    def test_start_rejects_collision_with_existing_file(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        upload_library_file(conn, workspace, project, filename="taken.pdf", content_base64=_PDF_B64)
        with pytest.raises(DocumentExistsError):
            start_library_file_upload(
                conn, workspace, project, filename="taken.pdf", total_size=10, total_chunks=1
            )

    def test_start_rejects_collision_with_existing_metadata_sidecar(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        sidecar = workspace / "projects" / project / "library" / "taken.json"
        sidecar.write_text('{"keep": true}\n', encoding="utf-8")

        with pytest.raises(DocumentExistsError):
            start_library_file_upload(
                conn,
                workspace,
                project,
                filename="taken.pdf",
                total_size=10,
                total_chunks=1,
            )

    def test_start_rejects_a_second_pending_session_for_the_same_target(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        start_library_file_upload(
            conn,
            workspace,
            project,
            filename="pending.pdf",
            total_size=10,
            total_chunks=1,
        )

        with pytest.raises(DocumentExistsError, match="already pending"):
            start_library_file_upload(
                conn,
                workspace,
                project,
                filename="pending.pdf",
                total_size=10,
                total_chunks=1,
            )

    def test_start_rejects_oversized_declared_total(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(writes, "MAX_UPLOAD_BYTES", 8)
        with pytest.raises(FileTooLargeError):
            start_library_file_upload(
                conn, workspace, project, filename="x.pdf", total_size=9, total_chunks=1
            )

    @pytest.mark.parametrize(
        ("total_size", "total_chunks"),
        ((True, 1), (1, True)),
    )
    def test_start_rejects_boolean_sizes(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        total_size: int,
        total_chunks: int,
    ) -> None:
        with pytest.raises(ValidationError):
            start_library_file_upload(
                conn,
                workspace,
                project,
                filename="x.pdf",
                total_size=total_size,
                total_chunks=total_chunks,
            )

    def test_start_caps_pending_sessions(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(writes, "MAX_PENDING_UPLOAD_SESSIONS_PER_PROJECT", 1)
        start_library_file_upload(
            conn, workspace, project, filename="one.pdf", total_size=1, total_chunks=1
        )
        with pytest.raises(ValidationError, match="Too many pending"):
            start_library_file_upload(
                conn, workspace, project, filename="two.pdf", total_size=1, total_chunks=1
            )

    def test_start_caps_pending_reserved_bytes(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(writes, "MAX_PENDING_UPLOAD_BYTES_PER_PROJECT", 4)
        start_library_file_upload(
            conn, workspace, project, filename="one.pdf", total_size=4, total_chunks=1
        )
        with pytest.raises(FileTooLargeError, match="reservation limit"):
            start_library_file_upload(
                conn, workspace, project, filename="two.pdf", total_size=1, total_chunks=1
            )

    def test_unknown_upload_id_rejected(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(OperationNotFoundError):
            append_upload_chunk(
                conn,
                workspace,
                project,
                upload_id="op_doesnotexist",
                chunk_index=0,
                chunk_base64=_PDF_B64,
            )

    def test_upload_scoped_to_its_project(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        from lattice.core.writes import create_project

        create_project(conn, workspace, key="other", title="Other")
        session = start_library_file_upload(
            conn, workspace, project, filename="x.pdf", total_size=10, total_chunks=1
        )
        with pytest.raises(PatchProjectMismatchError):
            append_upload_chunk(
                conn,
                workspace,
                "other",
                upload_id=session.upload_id,
                chunk_index=0,
                chunk_base64=_PDF_B64,
            )

    def test_expired_session_rejected(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        session = start_library_file_upload(
            conn, workspace, project, filename="x.pdf", total_size=1, total_chunks=1
        )
        append_upload_chunk(
            conn,
            workspace,
            project,
            upload_id=session.upload_id,
            chunk_index=0,
            chunk_base64=base64.b64encode(b"x").decode("ascii"),
        )
        staging = workspace / "projects" / project / ".lattice" / "uploads" / session.upload_id
        assert staging.is_dir()
        conn.execute(
            "UPDATE operations SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (session.upload_id,),
        )
        conn.commit()
        with pytest.raises(PatchExpiredError):
            append_upload_chunk(
                conn,
                workspace,
                project,
                upload_id=session.upload_id,
                chunk_index=0,
                chunk_base64=base64.b64encode(b"x").decode("ascii"),
            )
        assert not staging.exists()

    def test_start_cleans_expired_session_staging(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        expired = start_library_file_upload(
            conn, workspace, project, filename="old.pdf", total_size=1, total_chunks=1
        )
        append_upload_chunk(
            conn,
            workspace,
            project,
            upload_id=expired.upload_id,
            chunk_index=0,
            chunk_base64=base64.b64encode(b"x").decode("ascii"),
        )
        staging = workspace / "projects" / project / ".lattice" / "uploads" / expired.upload_id
        conn.execute(
            "UPDATE operations SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (expired.upload_id,),
        )
        conn.commit()

        start_library_file_upload(
            conn, workspace, project, filename="new.pdf", total_size=1, total_chunks=1
        )

        assert not staging.exists()

    def test_discard_cleans_up_and_blocks_further_use(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        session = start_library_file_upload(
            conn, workspace, project, filename="x.pdf", total_size=4, total_chunks=1
        )
        append_upload_chunk(
            conn,
            workspace,
            project,
            upload_id=session.upload_id,
            chunk_index=0,
            chunk_base64=base64.b64encode(b"xxxx").decode("ascii"),
        )
        result = discard_upload(conn, workspace, project, upload_id=session.upload_id)
        assert result.state == "discarded"

        staging = workspace / "projects" / project / ".lattice" / "uploads" / session.upload_id
        assert not staging.exists()

        with pytest.raises(InvalidOperationError):
            finalize_library_file_upload(conn, workspace, project, upload_id=session.upload_id)

    def test_discard_cleanup_failure_does_not_claim_success(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = start_library_file_upload(
            conn,
            workspace,
            project,
            filename="x.pdf",
            total_size=1,
            total_chunks=1,
        )

        def fail_cleanup(_project_dir: Path, _upload_id: str) -> None:
            raise OSError("synthetic cleanup failure")

        monkeypatch.setattr(upload_staging, "remove_staging_dir", fail_cleanup)
        with pytest.raises(OSError, match="synthetic cleanup failure"):
            discard_upload(
                conn,
                workspace,
                project,
                upload_id=session.upload_id,
            )

        operation = get_operation(conn, session.upload_id)
        assert operation is not None
        assert operation.state == "pending"
