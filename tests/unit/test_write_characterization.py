"""Characterization of the four write domains, pinned before REL-024 to REL-027.

These tests exist to make a *behaviour-preserving* extraction provably
behaviour-preserving. Each one fixes what a caller can observe about one
transaction — the returned model's field values, the operation-log row it
writes, the snapshot it takes, the index state it leaves, and the recovery
``details`` it hands back when it refuses. They must pass unchanged before and
after every extraction; a red test here means the move changed the contract.

**What is deliberately not repeated here.** ``tests/unit/test_writes.py``,
``test_uploads.py`` and ``test_chatgpt_upload.py`` already cover mid-transaction
rollback under injected failure for every domain
(``test_apply_bookkeeping_failure_restores_document_and_pending_proposal``,
``test_archive_bookkeeping_failure_rolls_back_the_move``,
``test_unarchive_bookkeeping_failure_rolls_back_the_move``,
``test_create_project_bookkeeping_failure_rolls_back_registry_and_tree``,
``test_durable_bookkeeping_failure_rolls_back_uploaded_files_and_snapshot``,
``test_finalize_terminal_state_failure_rolls_back_published_content``), and they
pin error *classes* — which pin the wire ``error_code``, since it is a
``ClassVar`` on the exception. Duplicating any of that would cost suite time and
buy nothing.

The gaps this file closes are the ones nothing else holds:

* the ``operation_type`` and terminal state each transaction writes — the
  strings ``operation_log`` shows a user, easy to change while moving code,
* the ``details`` payloads on refusal paths — 44 of them in ``writes.py`` and,
  before this file, not one assertion anywhere,
* the exact field values of ``WriteResult`` / ``ProposalResult`` / ``UploadResult``
  / ``ArchiveResult`` / ``CreateProjectResult``, including the diff text,
* index state after a create, after an archive, and for a non-Markdown upload.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from ferumind.core import patch_writes, upload_writes
from ferumind.core.document_writes import capture_note, create_document
from ferumind.core.documents import compute_sha256
from ferumind.core.errors import (
    DocumentArchivedError,
    FerumindError,
    FileTooLargeError,
    FrontmatterProtectedError,
    InvalidOperationError,
    UnknownFolderError,
    UnsupportedFileTypeError,
    ValidationError,
)
from ferumind.core.folders import CREATABLE_FOLDERS
from ferumind.core.frontmatter import parse_frontmatter
from ferumind.core.lifecycle_writes import (
    archive_document,
    restore_snapshot,
    unarchive_document,
)
from ferumind.core.operations import (
    OP_APPLIED,
    OP_PENDING,
    get_operation,
    list_operations,
)
from ferumind.core.patch_writes import (
    apply_patch,
    propose_exact_replace_patch,
)
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.project_writes import create_project
from ferumind.core.search import search_project
from ferumind.core.snapshots import find_snapshot_dir, list_snapshots_from_db
from ferumind.core.types import JsonObject
from ferumind.core.upload_writes import (
    start_library_file_upload,
    upload_library_file,
)
from ferumind.core.write_limits import (
    MAX_CHUNK_BYTES,
    MAX_UPLOAD_CHUNKS,
    MAX_UPLOAD_METADATA_BYTES,
)
from tests.conftest import TEST_DESCRIPTION


@pytest.fixture
def doc(conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str) -> str:
    """A canvas document with known content, returned as its project-relative path."""
    result = create_document(
        conn,
        workspace,
        project,
        folder_path="canvases",
        title="Plan",
        content="alpha\nbravo\ncharlie\n",
        description=TEST_DESCRIPTION,
    )
    return result.path


def _refusal_details(error: type[FerumindError], call: Callable[[], object]) -> JsonObject:
    """Return the ``details`` payload *call* refuses with.

    A refusal without ``details`` is itself a contract change: the payload is
    how a client corrects the call without another round trip (spec-mcp §7).
    """
    with pytest.raises(error) as excinfo:
        call()
    assert excinfo.value.details is not None, "refusal dropped its recovery details"
    return excinfo.value.details


def _project_dir(workspace: WorkspaceRoot, project: str) -> Path:
    return Path(workspace) / "projects" / project


class TestProposalAndApplyTransaction:
    """REL-024. Propose stages; only apply writes."""

    def test_propose_stages_a_pending_operation_and_writes_nothing(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        before = (_project_dir(workspace, project) / doc).read_text(encoding="utf-8")
        snapshots_before = len(list_snapshots_from_db(conn, project_key=project))

        result = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="bravo", new_string="BRAVO"
        )

        assert result.operation_id.startswith("op_")
        assert result.project_key == project
        assert result.path == doc
        assert result.folder == "canvases"
        assert result.proposal_kind == "exact_replace"
        assert result.document_before_sha256 == compute_sha256(before)
        assert result.after_sha256 != result.document_before_sha256
        assert result.deduped is False
        assert result.expires_at
        # The diff is caller-visible output, not a debugging aid.
        assert "-bravo" in result.diff
        assert "+BRAVO" in result.diff
        assert doc in result.diff

        record = get_operation(conn, result.operation_id)
        assert record is not None
        assert record.operation_type == "propose_exact_replace_patch"
        assert record.state == OP_PENDING
        assert record.target_path == doc
        assert record.project_key == project

        # Nothing was written or snapshotted.
        assert (_project_dir(workspace, project) / doc).read_text(encoding="utf-8") == before
        assert len(list_snapshots_from_db(conn, project_key=project)) == snapshots_before

    def test_apply_commits_the_edit_and_records_the_transaction(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="bravo", new_string="BRAVO"
        )
        result = apply_patch(conn, workspace, project, proposal.operation_id)

        on_disk = (_project_dir(workspace, project) / doc).read_text(encoding="utf-8")
        assert "BRAVO" in on_disk
        assert result.path == doc
        assert result.document_sha256 == compute_sha256(on_disk)
        assert result.document_sha256 == proposal.after_sha256, (
            "the hash a propose promised must be the hash apply delivers"
        )
        assert result.snapshot_id is not None
        assert result.index_error is None

        record = get_operation(conn, result.operation_id)
        assert record is not None
        assert record.operation_type == "apply_patch"
        assert record.state == OP_APPLIED
        assert record.snapshot_id == result.snapshot_id

        # The proposal itself reaches a terminal state — it cannot be replayed.
        staged = get_operation(conn, proposal.operation_id)
        assert staged is not None
        assert staged.state != OP_PENDING

        # The snapshot is registered and its directory exists on disk.
        snapshot_ids = {s.id for s in list_snapshots_from_db(conn, project_key=project)}
        assert result.snapshot_id in snapshot_ids
        assert find_snapshot_dir(_project_dir(workspace, project), result.snapshot_id) is not None

    def test_edit_refusals_carry_the_details_a_client_needs_to_recover(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        archived = archive_document(conn, workspace, project, path=doc)
        archived_details = _refusal_details(
            DocumentArchivedError,
            lambda: propose_exact_replace_patch(
                conn,
                workspace,
                project,
                path=archived.archived_path,
                old_string="bravo",
                new_string="x",
            ),
        )
        assert archived_details == {"path": archived.archived_path, "status": "archived"}

        fresh = create_document(
            conn,
            workspace,
            project,
            folder_path="canvases",
            title="Notes",
            content="one\n",
            description=TEST_DESCRIPTION,
        )
        protected = _refusal_details(
            FrontmatterProtectedError,
            lambda: patch_writes.propose_frontmatter_patch(
                conn,
                workspace,
                project,
                path=fresh.path,
                set_values={"id": "forged"},
                remove_keys=[],
            ),
        )
        assert protected == {"protected_keys": ["id"]}

        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=fresh.path, old_string="one", new_string="two"
        )
        apply_patch(conn, workspace, project, proposal.operation_id)
        replay = _refusal_details(
            InvalidOperationError,
            lambda: apply_patch(conn, workspace, project, proposal.operation_id),
        )
        assert replay == {"state": OP_APPLIED}, "a replayed apply must name the state it found"


class TestDocumentCreationAndCaptureTransaction:
    """REL-025. New documents are published, indexed, and logged."""

    def test_create_document_publishes_indexes_and_logs(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        result = create_document(
            conn,
            workspace,
            project,
            folder_path="canvases",
            title="Launch Plan",
            content="the body\n",
            description=TEST_DESCRIPTION,
        )

        assert result.path == "canvases/launch-plan.md"
        assert result.folder == "canvases"
        on_disk = (_project_dir(workspace, project) / result.path).read_text(encoding="utf-8")
        assert result.document_sha256 == compute_sha256(on_disk)
        assert result.index_error is None

        parsed = parse_frontmatter(on_disk)
        assert parsed["project"] == project
        assert parsed["title"] == "Launch Plan"

        record = get_operation(conn, result.operation_id)
        assert record is not None
        assert record.operation_type == "create_document"
        assert record.state == OP_APPLIED
        assert record.target_path == result.path

        hits = search_project(conn, project, "body")
        assert result.path in {hit.path for hit in hits}, "a created document must be searchable"

    def test_capture_note_lands_in_the_inbox_under_its_own_operation_type(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        result = capture_note(conn, workspace, project, text="a thought worth keeping")

        assert result.path.startswith("inbox/")
        assert result.folder == "inbox"
        record = get_operation(conn, result.operation_id)
        assert record is not None
        assert record.operation_type == "capture_note", (
            "capture_note must stay distinguishable from create_document in the log"
        )
        assert record.state == OP_APPLIED

    def test_folder_refusals_enumerate_the_folders_a_caller_may_use(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        details = _refusal_details(
            UnknownFolderError,
            lambda: create_document(
                conn,
                workspace,
                project,
                folder_path="nowhere",
                title="X",
                content="y\n",
                description=TEST_DESCRIPTION,
            ),
        )
        assert details == {"allowed_folders": list(CREATABLE_FOLDERS)}
        assert "canvases" in CREATABLE_FOLDERS


class TestUploadTransaction:
    """REL-026. Bytes land beside a sidecar, snapshotted and logged, never indexed."""

    def test_direct_upload_writes_bytes_a_sidecar_and_a_snapshot(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        payload = b"binary characterization payload"
        result = upload_library_file(
            conn,
            workspace,
            project,
            filename="sample.bin",
            content_base64=base64.b64encode(payload).decode("ascii"),
        )

        assert result.path == "library/sample.bin"
        assert result.metadata_path == "library/sample.json"
        assert result.size_bytes == len(payload)
        assert result.sha256 == compute_sha256(payload.decode("ascii"))
        stored = _project_dir(workspace, project) / result.path
        assert stored.read_bytes() == payload

        sidecar = json.loads(
            (_project_dir(workspace, project) / result.metadata_path).read_text(encoding="utf-8")
        )
        assert sidecar["sha256"] == result.sha256
        assert sidecar["size_bytes"] == result.size_bytes

        record = get_operation(conn, result.operation_id)
        assert record is not None
        assert record.operation_type == "upload_library_file"
        assert record.state == OP_APPLIED
        assert record.snapshot_id == result.snapshot_id

        assert search_project(conn, project, "characterization") == [], (
            "non-Markdown uploads are never indexed or searchable by content"
        )

    def test_upload_refusals_report_the_bound_they_broke(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        blocked = _refusal_details(
            UnsupportedFileTypeError,
            lambda: upload_library_file(
                conn,
                workspace,
                project,
                filename="payload.sh",
                content_base64=base64.b64encode(b"#!/bin/sh\n").decode("ascii"),
            ),
        )
        assert blocked == {"extension": ".sh"}

        oversized_metadata = _refusal_details(
            FileTooLargeError,
            lambda: upload_library_file(
                conn,
                workspace,
                project,
                filename="ok.bin",
                content_base64=base64.b64encode(b"x").decode("ascii"),
                metadata={"note": "y" * (MAX_UPLOAD_METADATA_BYTES + 1)},
            ),
        )
        assert oversized_metadata == {
            "max_bytes": MAX_UPLOAD_METADATA_BYTES,
            "scope": "metadata",
        }

        too_many_chunks = _refusal_details(
            ValidationError,
            lambda: start_library_file_upload(
                conn,
                workspace,
                project,
                filename="big.bin",
                total_size=1024,
                total_chunks=MAX_UPLOAD_CHUNKS + 1,
            ),
        )
        assert too_many_chunks == {
            "total_chunks": MAX_UPLOAD_CHUNKS + 1,
            "max_chunks": MAX_UPLOAD_CHUNKS,
        }

        outside_library = _refusal_details(
            UnknownFolderError,
            lambda: upload_library_file(
                conn,
                workspace,
                project,
                filename="ok.bin",
                content_base64=base64.b64encode(b"x").decode("ascii"),
                folder_path="canvases",
            ),
        )
        assert outside_library == {"allowed_folders": ["library"]}

    def test_a_chunked_session_is_a_pending_operation_until_it_finalizes(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        session = start_library_file_upload(
            conn,
            workspace,
            project,
            filename="streamed.bin",
            total_size=6,
            total_chunks=1,
        )
        assert session.upload_id
        assert session.chunk_size_hint == MAX_CHUNK_BYTES

        staged = get_operation(conn, session.upload_id)
        assert staged is not None
        assert staged.operation_type == upload_writes.UPLOAD_SESSION_OP_TYPE
        assert staged.state == OP_PENDING

        upload_writes.append_upload_chunk(
            conn,
            workspace,
            project,
            upload_id=session.upload_id,
            chunk_index=0,
            chunk_base64=base64.b64encode(b"abcdef").decode("ascii"),
        )
        result = upload_writes.finalize_library_file_upload(
            conn, workspace, project, upload_id=session.upload_id
        )

        assert result.path == "library/streamed.bin"
        assert (_project_dir(workspace, project) / result.path).read_bytes() == b"abcdef"
        finalized = get_operation(conn, result.operation_id)
        assert finalized is not None
        assert finalized.operation_type == "finalize_library_file_upload"
        assert finalized.state == OP_APPLIED


class TestArchiveRestoreAndProjectAdminTransaction:
    """REL-027. Lifecycle moves, snapshot restores, and project publication."""

    def test_archive_moves_sets_status_and_drops_the_document_from_search(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        assert doc in {hit.path for hit in search_project(conn, project, "bravo")}

        result = archive_document(conn, workspace, project, path=doc)

        assert result.path == doc
        assert result.archived_path == f"archive/{doc}"
        assert result.index_error is None
        assert not (_project_dir(workspace, project) / doc).exists()
        archived_text = (_project_dir(workspace, project) / result.archived_path).read_text(
            encoding="utf-8"
        )
        assert parse_frontmatter(archived_text)["status"] == "archived"
        assert result.document_sha256 == compute_sha256(archived_text)

        record = get_operation(conn, result.operation_id)
        assert record is not None
        assert record.operation_type == "archive_document"
        assert record.state == OP_APPLIED
        assert record.snapshot_id == result.snapshot_id

        assert doc not in {hit.path for hit in search_project(conn, project, "bravo")}

    def test_unarchive_returns_the_document_and_logs_its_own_type(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        archived = archive_document(conn, workspace, project, path=doc)
        result = unarchive_document(conn, workspace, project, archived_path=archived.archived_path)

        assert result.path == doc, "path is where the document now lives"
        assert result.archived_path == archived.archived_path
        restored = (_project_dir(workspace, project) / doc).read_text(encoding="utf-8")
        assert parse_frontmatter(restored)["status"] == "active"

        record = get_operation(conn, result.operation_id)
        assert record is not None
        assert record.operation_type == "unarchive_document"
        assert record.state == OP_APPLIED
        assert doc in {hit.path for hit in search_project(conn, project, "bravo")}

    def test_restore_snapshots_the_current_state_before_rolling_back(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        original = (_project_dir(workspace, project) / doc).read_text(encoding="utf-8")
        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="bravo", new_string="BRAVO"
        )
        applied = apply_patch(conn, workspace, project, proposal.operation_id)
        assert applied.snapshot_id is not None

        result = restore_snapshot(conn, workspace, project, applied.snapshot_id)

        assert result.restored_from_snapshot_id == applied.snapshot_id
        assert result.rollback_snapshot_id is not None, (
            "restore must snapshot what it overwrites, or the restore is not reversible"
        )
        assert result.rollback_snapshot_id != applied.snapshot_id
        assert (_project_dir(workspace, project) / doc).read_text(encoding="utf-8") == original

        record = get_operation(conn, result.operation_id)
        assert record is not None
        assert record.operation_type == "restore_snapshot"
        assert record.state == OP_APPLIED

    def test_create_project_seeds_registers_and_logs_under_its_own_key(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot
    ) -> None:
        result = create_project(conn, workspace, key="characterized", title="Characterized")

        assert result.key == "characterized"
        assert result.title == "Characterized"
        assert result.seeded, "a new project must report what it seeded"
        assert "spine.md" in result.seeded
        assert (Path(workspace) / "projects/characterized/spine.md").is_file()

        record = get_operation(conn, result.operation_id)
        assert record is not None
        assert record.operation_type == "create_project"
        assert record.state == OP_APPLIED
        assert record.project_key == "characterized"
        assert record.snapshot_id == result.snapshot_id


class TestTransactionLedger:
    """One place that names every operation type the write domains emit.

    ``operation_log`` shows these strings to users, so they are public surface.
    Renaming one while moving code is a silent break; this test is where it
    stops being silent.
    """

    def test_each_domain_writes_its_own_operation_type(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="bravo", new_string="BRAVO"
        )
        apply_patch(conn, workspace, project, proposal.operation_id)
        capture_note(conn, workspace, project, text="ledger note")
        upload_library_file(
            conn,
            workspace,
            project,
            filename="ledger.bin",
            content_base64=base64.b64encode(b"bytes").decode("ascii"),
        )
        archived = archive_document(conn, workspace, project, path=doc)
        unarchive_document(conn, workspace, project, archived_path=archived.archived_path)

        emitted = {record.operation_type for record in list_operations(conn, project, limit=200)}
        assert {
            "propose_exact_replace_patch",
            "apply_patch",
            "create_document",
            "capture_note",
            "upload_library_file",
            "archive_document",
            "unarchive_document",
        } <= emitted

    def test_every_committed_transaction_is_logged_against_a_snapshot(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        """No mutation may reach disk without a snapshot to undo it (AGENTS.md)."""
        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="bravo", new_string="BRAVO"
        )
        apply_patch(conn, workspace, project, proposal.operation_id)
        upload_library_file(
            conn,
            workspace,
            project,
            filename="guarded.bin",
            content_base64=base64.b64encode(b"bytes").decode("ascii"),
        )
        archive_document(conn, workspace, project, path=doc)

        # A proposal record also reaches ``applied`` — that is how apply_patch
        # retires it — but it never wrote bytes, so it carries no snapshot. The
        # invariant belongs to the operations that touched the filesystem.
        mutating = {
            "apply_patch",
            "create_document",
            "capture_note",
            "upload_library_file",
            "finalize_library_file_upload",
            "archive_document",
            "unarchive_document",
            "restore_snapshot",
            "create_project",
        }
        committed = [
            record
            for record in list_operations(conn, project, limit=200)
            if record.operation_type in mutating and record.state == OP_APPLIED
        ]
        assert committed, "the transactions above must appear in the log"
        unprotected = [record for record in committed if record.snapshot_id is None]
        assert unprotected == [], (
            "these committed operations carry no snapshot id: "
            f"{[r.operation_type for r in unprotected]}"
        )
