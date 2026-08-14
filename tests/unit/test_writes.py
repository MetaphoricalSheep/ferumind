"""Tests for the central write service: propose → apply, lifecycle, refusals."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ferumind.core import lifecycle_writes as lifecycle_writes_module
from ferumind.core import patch_writes as patch_writes_module
from ferumind.core import project_writes as project_writes_module
from ferumind.core.document_writes import CAPTURE_NOTE_DESCRIPTION, capture_note, create_document
from ferumind.core.documents import compute_sha256, parse_document_content
from ferumind.core.errors import (
    CannotArchiveSpineError,
    DocumentArchivedError,
    DocumentExistsError,
    DocumentNotFoundError,
    FrontmatterInvalidError,
    FrontmatterProtectedError,
    InvalidOperationError,
    OperationNotFoundError,
    PatchConflictError,
    PatchExpiredError,
    PatchProjectMismatchError,
    PathExistsError,
    ProjectNotFoundError,
    SnapshotNotFoundError,
    UnknownFolderError,
    ValidationError,
    WorkspaceMismatchError,
)
from ferumind.core.frontmatter import MAX_DESCRIPTION_CHARS, parse_frontmatter
from ferumind.core.lifecycle_writes import (
    archive_document,
    restore_snapshot,
    unarchive_document,
)
from ferumind.core.locks import acquire_project_lock
from ferumind.core.operations import get_operation
from ferumind.core.patch_writes import (
    apply_patch,
    discard_patch,
    propose_exact_replace_patch,
    propose_frontmatter_patch,
    propose_patch,
)
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.project_writes import create_project
from ferumind.core.registry import ProjectEntry, load_registry, require_project, save_registry
from ferumind.core.snapshots import (
    create_snapshot,
    find_snapshot_dir,
    new_snapshot_id,
    read_snapshot_before_content,
    record_snapshot_in_db,
)
from ferumind.core.write_limits import MAX_TITLE_CHARS
from ferumind.db.database import Database
from tests.conftest import TEST_DESCRIPTION


@pytest.fixture
def doc(conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str) -> str:
    result = create_document(
        conn,
        workspace,
        project,
        folder_path="canvases",
        title="Plan",
        content="# Plan\n\nalpha beta gamma\n",
        description=TEST_DESCRIPTION,
    )
    return result.path


def _read(workspace: WorkspaceRoot, project: str, rel: str) -> str:
    return (workspace / "projects" / project / rel).read_text(encoding="utf-8")


class TestProposeApply:
    def test_propose_is_not_a_saved_edit(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        before = _read(workspace, project, doc)
        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="alpha", new_string="ALPHA"
        )
        assert _read(workspace, project, doc) == before
        assert proposal.operation_id.startswith("op_")
        assert proposal.expires_at
        assert proposal.policy.edit_policy == "free"
        assert "ALPHA" in proposal.diff

    def test_apply_saves_and_returns_chainable_hash(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="alpha", new_string="ALPHA"
        )
        result = apply_patch(conn, workspace, project, proposal.operation_id)
        content = _read(workspace, project, doc)
        assert "ALPHA" in content
        assert result.document_sha256 == compute_sha256(content)
        assert result.snapshot_id
        # Hash chaining: the returned hash guards the next edit without re-reading.
        second = propose_exact_replace_patch(
            conn,
            workspace,
            project,
            path=doc,
            old_string="beta",
            new_string="BETA",
            expected_document_sha256=result.document_sha256,
        )
        apply_patch(conn, workspace, project, second.operation_id)
        assert "BETA" in _read(workspace, project, doc)

    def test_apply_bookkeeping_failure_restores_document_and_pending_proposal(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        doc: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        before = _read(workspace, project, doc)
        proposal = propose_exact_replace_patch(
            conn,
            workspace,
            project,
            path=doc,
            old_string="alpha",
            new_string="ALPHA",
        )
        snapshots_root = workspace / "projects" / project / ".ferumind" / "snapshots"
        before_dirs: set[Path] = (
            {Path(entry) for entry in snapshots_root.iterdir()}
            if snapshots_root.is_dir()
            else set()
        )
        before_snapshot_rows = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        before_operation_rows = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]

        def fail(*_args: object, **_kwargs: object) -> str:
            raise sqlite3.OperationalError("synthetic apply bookkeeping failure")

        # patch_writes, not writes: apply_patch's bookkeeping now runs there,
        # and writes still re-reads its own record_operation for other domains.
        monkeypatch.setattr(patch_writes_module, "record_operation", fail)
        with pytest.raises(sqlite3.OperationalError, match="synthetic"):
            apply_patch(conn, workspace, project, proposal.operation_id)

        assert _read(workspace, project, doc) == before
        pending = get_operation(conn, proposal.operation_id)
        assert pending is not None
        assert pending.state == "pending"
        assert pending.request_json["new_content"]
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

    def test_apply_rejects_corrupted_prepared_content_before_mutation(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        doc: str,
    ) -> None:
        before = _read(workspace, project, doc)
        proposal = propose_exact_replace_patch(
            conn,
            workspace,
            project,
            path=doc,
            old_string="alpha",
            new_string="ALPHA",
        )
        conn.execute(
            "UPDATE operations SET request_json = ? WHERE id = ?",
            (
                json.dumps({"new_content": before + "\nsubstituted\n"}),
                proposal.operation_id,
            ),
        )
        conn.commit()

        with pytest.raises(InvalidOperationError, match="integrity"):
            apply_patch(conn, workspace, project, proposal.operation_id)

        assert _read(workspace, project, doc) == before

    def test_apply_refreshes_updated_timestamp(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        original_updated = next(
            line
            for line in _read(workspace, project, doc).splitlines()
            if line.startswith("updated:")
        )
        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="alpha", new_string="ALPHA"
        )
        apply_patch(conn, workspace, project, proposal.operation_id)
        new_updated = next(
            line
            for line in _read(workspace, project, doc).splitlines()
            if line.startswith("updated:")
        )
        assert new_updated >= original_updated

    def test_double_apply_is_invalid_operation(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="alpha", new_string="ALPHA"
        )
        apply_patch(conn, workspace, project, proposal.operation_id)
        with pytest.raises(InvalidOperationError):
            apply_patch(conn, workspace, project, proposal.operation_id)

    def test_apply_unknown_and_non_proposal_operations(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        with pytest.raises(OperationNotFoundError):
            apply_patch(conn, workspace, project, "op_does-not-exist")
        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="alpha", new_string="ALPHA"
        )
        applied = apply_patch(conn, workspace, project, proposal.operation_id)
        with pytest.raises(InvalidOperationError):
            apply_patch(conn, workspace, project, applied.operation_id)

    def test_apply_from_wrong_project_is_refused(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        create_project(conn, workspace, key="other", title="Other")
        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="alpha", new_string="ALPHA"
        )
        with pytest.raises(PatchProjectMismatchError):
            apply_patch(conn, workspace, "other", proposal.operation_id)

    def test_out_of_band_edit_between_propose_and_apply_fails_closed(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="alpha", new_string="ALPHA"
        )
        target = workspace / "projects" / project / doc
        hand_edited = target.read_text(encoding="utf-8") + "\nout-of-band\n"
        target.write_text(hand_edited, encoding="utf-8")
        with pytest.raises(PatchConflictError):
            apply_patch(conn, workspace, project, proposal.operation_id)
        # Never clobbers: the hand edit survives.
        assert _read(workspace, project, doc) == hand_edited

    def test_expired_proposal_fails_with_patch_expired(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="alpha", new_string="ALPHA"
        )
        past = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        conn.execute(
            "UPDATE operations SET expires_at = ? WHERE id = ?", (past, proposal.operation_id)
        )
        conn.commit()
        with pytest.raises(PatchExpiredError):
            apply_patch(conn, workspace, project, proposal.operation_id)

    def test_discard_then_apply_is_invalid(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="alpha", new_string="ALPHA"
        )
        result = discard_patch(conn, workspace, project, proposal.operation_id)
        assert result.state == "discarded"
        with pytest.raises(InvalidOperationError):
            apply_patch(conn, workspace, project, proposal.operation_id)
        with pytest.raises(InvalidOperationError):
            discard_patch(conn, workspace, project, proposal.operation_id)

    def test_discard_waits_for_the_same_project_lock_as_apply(
        self,
        conn: sqlite3.Connection,
        database: Database,
        workspace: WorkspaceRoot,
        project: str,
        doc: str,
    ) -> None:
        proposal = propose_exact_replace_patch(
            conn,
            workspace,
            project,
            path=doc,
            old_string="alpha",
            new_string="ALPHA",
        )
        project_dir = workspace / "projects" / project
        lock_acquired = threading.Event()
        release_lock = threading.Event()
        discard_started = threading.Event()
        discard_finished = threading.Event()
        errors: list[BaseException] = []

        def hold_lock() -> None:
            with acquire_project_lock(project_dir, project):
                lock_acquired.set()
                release_lock.wait(timeout=2)

        def discard() -> None:
            other_conn = database.get_connection()
            try:
                discard_started.set()
                discard_patch(
                    other_conn,
                    workspace,
                    project,
                    proposal.operation_id,
                )
            except BaseException as exc:  # surfaced in the main test thread
                errors.append(exc)
            finally:
                other_conn.close()
                discard_finished.set()

        holder = threading.Thread(target=hold_lock)
        worker = threading.Thread(target=discard)
        holder.start()
        assert lock_acquired.wait(timeout=1)
        worker.start()
        assert discard_started.wait(timeout=1)
        assert not discard_finished.wait(timeout=0.1)
        release_lock.set()
        holder.join(timeout=2)
        worker.join(timeout=2)

        assert discard_finished.is_set()
        assert errors == []
        with pytest.raises(InvalidOperationError):
            apply_patch(conn, workspace, project, proposal.operation_id)

    def test_equivalent_proposals_are_deduped(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        first = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="alpha", new_string="ALPHA"
        )
        second = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="alpha", new_string="ALPHA"
        )
        assert second.operation_id == first.operation_id
        assert second.deduped
        assert not first.deduped

    def test_different_replacements_are_never_deduped(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        first = propose_patch(
            conn,
            workspace,
            project,
            path=doc,
            new_content="# First\n",
        )
        second = propose_patch(
            conn,
            workspace,
            project,
            path=doc,
            new_content="# Second\n",
        )
        assert second.operation_id != first.operation_id
        assert not second.deduped

    def test_propose_against_archived_target_is_refused(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        archived = archive_document(conn, workspace, project, path=doc)
        with pytest.raises(DocumentArchivedError):
            propose_exact_replace_patch(
                conn,
                workspace,
                project,
                path=archived.archived_path,
                old_string="alpha",
                new_string="ALPHA",
            )

    def test_propose_outside_project_is_refused(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(WorkspaceMismatchError):
            propose_exact_replace_patch(
                conn,
                workspace,
                project,
                path="../../system/projects.yml",
                old_string="a",
                new_string="b",
            )

    def test_frontmatter_identity_keys_are_protected(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        with pytest.raises(FrontmatterProtectedError):
            propose_frontmatter_patch(
                conn, workspace, project, path=doc, set_values={"id": "doc_evil"}, remove_keys=[]
            )

    def test_frontmatter_patch_sets_behavior_keys(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        proposal = propose_frontmatter_patch(
            conn,
            workspace,
            project,
            path=doc,
            set_values={"edit_policy": "append"},
            remove_keys=[],
        )
        apply_patch(conn, workspace, project, proposal.operation_id)
        assert "edit_policy: append" in _read(workspace, project, doc)

    def test_frontmatter_patch_changes_description_through_guarded_apply(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        before = _read(workspace, project, doc)
        before_fm = parse_frontmatter(before)
        replacement = "Navigation sentence updated after the document purpose changed."
        proposal = propose_frontmatter_patch(
            conn,
            workspace,
            project,
            path=doc,
            set_values={"description": replacement},
            remove_keys=[],
            expected_document_sha256=compute_sha256(before),
        )

        applied = apply_patch(conn, workspace, project, proposal.operation_id)
        after = _read(workspace, project, doc)
        after_fm = parse_frontmatter(after)
        assert after_fm["description"] == replacement
        assert after_fm["updated"] != before_fm["updated"]
        assert applied.document_sha256 == compute_sha256(after)

    def test_propose_patch_body_mode_preserves_frontmatter(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        proposal = propose_patch(
            conn, workspace, project, path=doc, new_content="# Rewritten\n", mode="body"
        )
        apply_patch(conn, workspace, project, proposal.operation_id)
        content = _read(workspace, project, doc)
        assert content.startswith("---\n")
        assert "# Rewritten" in content
        assert "id: doc_" in content

    def test_propose_patch_full_mode_preserves_created_and_manages_updated(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        original = _read(workspace, project, doc)
        original_frontmatter = parse_frontmatter(original)
        replacement = original.replace("# Plan\n\nalpha beta gamma\n", "# Rewritten\n")

        proposal = propose_patch(
            conn,
            workspace,
            project,
            path=doc,
            new_content=replacement,
            mode="full",
        )
        apply_patch(conn, workspace, project, proposal.operation_id)

        updated_content = _read(workspace, project, doc)
        updated_frontmatter = parse_frontmatter(updated_content)
        assert updated_frontmatter["created"] == original_frontmatter["created"]
        assert datetime.fromisoformat(
            str(updated_frontmatter["updated"])
        ) >= datetime.fromisoformat(str(original_frontmatter["updated"]))
        assert "# Rewritten" in updated_content

    @pytest.mark.parametrize("protected_key", ["created", "updated"])
    def test_propose_patch_full_mode_rejects_protected_timestamp_changes(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        doc: str,
        protected_key: str,
    ) -> None:
        original = _read(workspace, project, doc)
        original_line = next(
            line for line in original.splitlines() if line.startswith(f"{protected_key}:")
        )
        tampered = original.replace(
            original_line,
            f"{protected_key}: 1999-01-01T00:00:00+00:00",
            1,
        )

        with pytest.raises(FrontmatterProtectedError):
            propose_patch(
                conn,
                workspace,
                project,
                path=doc,
                new_content=tampered,
                mode="full",
            )

    def test_propose_patch_full_mode_rejects_lexical_created_change(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        original = _read(workspace, project, doc)
        created_line = next(line for line in original.splitlines() if line.startswith("created:"))
        tampered = original.replace(created_line, created_line.replace("T", " ", 1), 1)

        with pytest.raises(FrontmatterProtectedError):
            propose_patch(
                conn,
                workspace,
                project,
                path=doc,
                new_content=tampered,
                mode="full",
            )

    def test_policy_echo_reports_frozen_and_ask_human(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        rules_proposal = propose_exact_replace_patch(
            conn,
            workspace,
            project,
            path="rules/00-project.md",
            old_string="Project rules",
            new_string="Project Rules",
        )
        assert rules_proposal.policy.edit_policy == "ask-human"
        assert rules_proposal.policy.policy_note is not None
        assert "human-owned" in rules_proposal.policy.policy_note


class TestDirectWrites:
    def test_create_document_nested_and_duplicate(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        result = create_document(
            conn,
            workspace,
            project,
            folder_path="library/runbooks",
            title="Rebuild Guide",
            content="steps\n",
            description=TEST_DESCRIPTION,
        )
        assert result.path == "library/runbooks/rebuild-guide.md"
        assert result.folder == "library"
        assert parse_frontmatter(_read(workspace, project, result.path))["description"] == (
            TEST_DESCRIPTION
        )
        with pytest.raises(DocumentExistsError):
            create_document(
                conn,
                workspace,
                project,
                folder_path="library/runbooks",
                title="Rebuild Guide",
                content="again\n",
                description=TEST_DESCRIPTION,
            )

    def test_create_document_rejects_unknown_and_archive_folders(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        for folder_path in ("docs", "archive", ""):
            with pytest.raises(UnknownFolderError):
                create_document(
                    conn,
                    workspace,
                    project,
                    folder_path=folder_path,
                    title="X",
                    content="x",
                    description=TEST_DESCRIPTION,
                )
        with pytest.raises(ValidationError):
            create_document(
                conn,
                workspace,
                project,
                folder_path="canvases",
                title="  ",
                content="x",
                description=TEST_DESCRIPTION,
            )

    @pytest.mark.parametrize(
        "folder_path",
        (
            "/canvases",
            "canvases/",
            "canvases//nested",
            r"canvases\nested",
            "canvases/../memory",
            " canvases",
            "canvases\n",
        ),
    )
    def test_create_document_rejects_noncanonical_folder_paths(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        folder_path: str,
    ) -> None:
        with pytest.raises(ValidationError):
            create_document(
                conn,
                workspace,
                project,
                folder_path=folder_path,
                title="X",
                content="x",
                description=TEST_DESCRIPTION,
            )

    def test_create_document_rejects_control_characters_in_title(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(ValidationError):
            create_document(
                conn,
                workspace,
                project,
                folder_path="canvases",
                title="bad\ntitle",
                content="x",
                description=TEST_DESCRIPTION,
            )

    def test_capture_note_lands_in_inbox(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        result = capture_note(conn, workspace, project, text="remember the tunnel cert")
        assert result.path.startswith("inbox/")
        content = _read(workspace, project, result.path)
        assert "remember the tunnel cert" in content
        assert parse_frontmatter(content)["description"] == CAPTURE_NOTE_DESCRIPTION
        with pytest.raises(ValidationError):
            capture_note(conn, workspace, project, text="   ")


class TestArchiveLifecycle:
    def test_round_trip_preserves_content_and_id(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        original = _read(workspace, project, doc)
        original_id = next(line for line in original.splitlines() if line.startswith("id:"))
        archived = archive_document(conn, workspace, project, path=doc)
        assert archived.archived_path == f"archive/{doc}"
        assert not (workspace / "projects" / project / doc).exists()
        archived_content = _read(workspace, project, archived.archived_path)
        assert "status: archived" in archived_content
        assert original_id in archived_content
        assert parse_frontmatter(archived_content)["description"] == TEST_DESCRIPTION

        restored = unarchive_document(
            conn, workspace, project, archived_path=archived.archived_path
        )
        assert restored.path == doc
        restored_content = _read(workspace, project, doc)
        assert "status: active" in restored_content
        assert original_id in restored_content
        assert parse_frontmatter(restored_content)["description"] == TEST_DESCRIPTION
        assert "alpha beta gamma" in restored_content

    def test_spine_cannot_be_archived(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(CannotArchiveSpineError):
            archive_document(conn, workspace, project, path="spine.md")

    def test_archive_bookkeeping_failure_rolls_back_the_move(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        doc: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        before = _read(workspace, project, doc)

        def fail(*_args: object, **_kwargs: object) -> str:
            raise sqlite3.OperationalError("synthetic archive bookkeeping failure")

        # `archive_document` now runs in `lifecycle_writes`; patching `writes`
        # would still succeed and do nothing (REL-025's silent seam class).
        monkeypatch.setattr(lifecycle_writes_module, "record_operation", fail)
        with pytest.raises(sqlite3.OperationalError, match="synthetic"):
            archive_document(conn, workspace, project, path=doc)

        assert _read(workspace, project, doc) == before
        assert not (workspace / "projects" / project / f"archive/{doc}").exists()

    def test_unarchive_bookkeeping_failure_rolls_back_the_move(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        doc: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        archived = archive_document(conn, workspace, project, path=doc)
        archived_before = _read(workspace, project, archived.archived_path)

        def fail(*_args: object, **_kwargs: object) -> str:
            raise sqlite3.OperationalError("synthetic unarchive bookkeeping failure")

        # `unarchive_document` now runs in `lifecycle_writes`.
        monkeypatch.setattr(lifecycle_writes_module, "record_operation", fail)
        with pytest.raises(sqlite3.OperationalError, match="synthetic"):
            unarchive_document(
                conn,
                workspace,
                project,
                archived_path=archived.archived_path,
            )

        assert _read(workspace, project, archived.archived_path) == archived_before
        assert not (workspace / "projects" / project / doc).exists()

    def test_archive_index_failure_does_not_report_the_committed_move_as_failed(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        doc: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []

        def fail_index_removal(*_args: object, **_kwargs: object) -> None:
            calls.append("remove_from_index")
            raise sqlite3.OperationalError("injected index removal failure")

        monkeypatch.setattr(lifecycle_writes_module, "remove_from_index", fail_index_removal)
        result = archive_document(conn, workspace, project, path=doc)

        # Without this the test still passes when the patch stops biting, and
        # would be asserting the happy path instead of the tolerated failure.
        assert calls == ["remove_from_index"], "the injected index failure never fired"
        assert result.index_error == "Index removal failed (OperationalError)"
        assert not (workspace / "projects" / project / doc).exists()
        assert (workspace / "projects" / project / result.archived_path).is_file()

    def test_double_archive_refused(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        archived = archive_document(conn, workspace, project, path=doc)
        with pytest.raises(DocumentArchivedError):
            archive_document(conn, workspace, project, path=archived.archived_path)

    def test_unarchive_collision_fails_with_path_exists(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        archived = archive_document(conn, workspace, project, path=doc)
        create_document(
            conn,
            workspace,
            project,
            folder_path="canvases",
            title="Plan",
            content="new one\n",
            description=TEST_DESCRIPTION,
        )
        with pytest.raises(PathExistsError):
            unarchive_document(conn, workspace, project, archived_path=archived.archived_path)

    def test_archive_missing_document(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(DocumentNotFoundError):
            archive_document(conn, workspace, project, path="canvases/ghost.md")


class TestRestore:
    def test_restore_returns_pre_edit_content(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, doc: str
    ) -> None:
        before = _read(workspace, project, doc)
        proposal = propose_exact_replace_patch(
            conn, workspace, project, path=doc, old_string="alpha", new_string="ALPHA"
        )
        applied = apply_patch(conn, workspace, project, proposal.operation_id)
        assert applied.snapshot_id is not None
        result = restore_snapshot(conn, workspace, project, applied.snapshot_id)
        assert _read(workspace, project, doc) == before
        assert result.rollback_snapshot_id is not None
        op = get_operation(conn, result.operation_id)
        assert op is not None
        assert op.operation_type == "restore_snapshot"

    def test_restore_rejects_tampered_snapshot_content_before_mutation(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        doc: str,
    ) -> None:
        proposal = propose_exact_replace_patch(
            conn,
            workspace,
            project,
            path=doc,
            old_string="alpha",
            new_string="ALPHA",
        )
        applied = apply_patch(conn, workspace, project, proposal.operation_id)
        assert applied.snapshot_id is not None
        current = _read(workspace, project, doc)
        snapshot_dir = find_snapshot_dir(
            workspace / "projects" / project,
            applied.snapshot_id,
        )
        assert snapshot_dir is not None
        (snapshot_dir / "before" / doc).write_text(
            current + "\nsubstituted snapshot\n",
            encoding="utf-8",
        )
        before_snapshot_rows = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]

        with pytest.raises(SnapshotNotFoundError, match="integrity"):
            restore_snapshot(conn, workspace, project, applied.snapshot_id)

        assert _read(workspace, project, doc) == current
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == before_snapshot_rows

    def test_restore_rejects_legacy_snapshot_missing_current_description(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        doc: str,
    ) -> None:
        current = _read(workspace, project, doc)
        legacy_content = (
            "\n".join(line for line in current.splitlines() if not line.startswith("description:"))
            + "\n"
        )
        snapshot_id = new_snapshot_id()
        snapshot_dir = create_snapshot(
            workspace / "projects" / project,
            project_key=project,
            target_path=doc,
            before_content=legacy_content,
            after_content=None,
            reason="test_legacy_restore",
            snapshot_id=snapshot_id,
        )
        record_snapshot_in_db(
            conn,
            snapshot_id=snapshot_id,
            project_key=project,
            target_path=doc,
            snapshot_dir=str(snapshot_dir),
            reason="test_legacy_restore",
        )
        before_snapshot_rows = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]

        with pytest.raises(FrontmatterInvalidError, match="description"):
            restore_snapshot(conn, workspace, project, snapshot_id)

        assert _read(workspace, project, doc) == current
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == before_snapshot_rows

    def test_restore_refuses_single_file_restore_of_archive_transition(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        doc: str,
    ) -> None:
        archived = archive_document(conn, workspace, project, path=doc)
        origin = workspace / "projects" / project / doc
        archived_file = workspace / "projects" / project / archived.archived_path

        with pytest.raises(InvalidOperationError, match="Archive transition"):
            restore_snapshot(conn, workspace, project, archived.snapshot_id)

        assert not origin.exists()
        assert archived_file.is_file()

    def test_restore_rollback_snapshot_preserves_existing_empty_file(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        target_path = "canvases/empty.md"
        target = workspace / "projects" / project / target_path
        target.write_text("", encoding="utf-8")
        source_snapshot_id = new_snapshot_id()
        source_snapshot_dir = create_snapshot(
            workspace / "projects" / project,
            project_key=project,
            target_path=target_path,
            before_content="restored\n",
            after_content=None,
            reason="test_restore",
            snapshot_id=source_snapshot_id,
        )
        record_snapshot_in_db(
            conn,
            snapshot_id=source_snapshot_id,
            project_key=project,
            target_path=target_path,
            snapshot_dir=str(source_snapshot_dir),
            reason="test_restore",
        )

        result = restore_snapshot(conn, workspace, project, source_snapshot_id)

        rollback_dir = find_snapshot_dir(
            workspace / "projects" / project, result.rollback_snapshot_id or ""
        )
        assert rollback_dir is not None
        assert read_snapshot_before_content(rollback_dir, target_path) == ""


class TestCreateProject:
    def test_create_project_seeds_and_registers(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot
    ) -> None:
        result = create_project(conn, workspace, key="garden", title="Garden")
        assert result.seeded == ["spine.md", "rules/00-project.md"]
        project_dir = workspace / "projects" / "garden"
        for sub in ("rules", "canvases", "memory", "library", "inbox", "archive"):
            assert (project_dir / sub).is_dir()
        spine = (project_dir / "spine.md").read_text(encoding="utf-8")
        assert "# Garden" in spine
        assert "id: doc_" in spine
        rules = (project_dir / "rules/00-project.md").read_text(encoding="utf-8")
        assert "edit_policy: ask-human" in rules
        assert parse_document_content(
            spine, project_key="garden", path="spine.md"
        ).description.startswith("Entry page for Garden")
        assert parse_document_content(
            rules, project_key="garden", path="rules/00-project.md"
        ).description.startswith("Human-owned rules specific to Garden")

    def test_create_project_refuses_a_missing_template_without_fallback(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot
    ) -> None:
        (workspace / "system/templates/spine.md").unlink()

        with pytest.raises(FrontmatterInvalidError, match="Required project template"):
            create_project(conn, workspace, key="missing-template", title="Missing Template")

        assert "missing-template" not in load_registry(workspace)
        assert not (workspace / "projects/missing-template").exists()

    @pytest.mark.parametrize(
        "replacement",
        [None, 'description: ""\n', "description: []\n", f"description: {'x' * 301}\n"],
        ids=["missing", "empty", "non-string", "too-long"],
    )
    def test_create_project_refuses_invalid_template_descriptions_without_fallback(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        replacement: str | None,
    ) -> None:
        template = workspace / "system/templates/spine.md"
        lines = template.read_text(encoding="utf-8").splitlines(keepends=True)
        description_line = next(line for line in lines if line.startswith("description:"))
        rewritten = "".join(
            replacement if line == description_line and replacement is not None else line
            for line in lines
            if line != description_line or replacement is not None
        )
        template.write_text(rewritten, encoding="utf-8")

        with pytest.raises(FrontmatterInvalidError):
            create_project(conn, workspace, key="bad-template", title="Bad Template")

        assert "bad-template" not in load_registry(workspace)
        assert not (workspace / "projects/bad-template").exists()

    def test_create_project_keeps_a_valid_long_title_with_bounded_descriptions(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot
    ) -> None:
        title = "L" * MAX_TITLE_CHARS

        create_project(conn, workspace, key="long-title", title=title)

        project_dir = workspace / "projects/long-title"
        for path in ("spine.md", "rules/00-project.md"):
            parsed = parse_document_content(
                (project_dir / path).read_text(encoding="utf-8"),
                project_key="long-title",
                path=path,
            )
            assert parsed.title == title
            assert "this project" in parsed.description
            assert len(parsed.description) <= MAX_DESCRIPTION_CHARS

    def test_create_project_refuses_duplicates_and_bad_keys(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot
    ) -> None:
        create_project(conn, workspace, key="garden", title="Garden")
        with pytest.raises(PathExistsError):
            create_project(conn, workspace, key="garden", title="Again")
        with pytest.raises(ValidationError):
            create_project(conn, workspace, key="Bad Key", title="Nope")
        with pytest.raises(ValidationError):
            create_project(conn, workspace, key="fine", title="   ")

    def test_create_project_bookkeeping_failure_rolls_back_registry_and_tree(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        before_registry = load_registry(workspace)

        def fail(*_args: object, **_kwargs: object) -> str:
            raise sqlite3.OperationalError("synthetic project bookkeeping failure")

        monkeypatch.setattr(project_writes_module, "record_operation", fail)
        with pytest.raises(sqlite3.OperationalError, match="synthetic"):
            create_project(conn, workspace, key="rollback", title="Rollback")

        assert load_registry(workspace) == before_registry
        assert not (workspace / "projects/rollback").exists()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM operations WHERE project_key = 'rollback'"
            ).fetchone()[0]
            == 0
        )

    def test_create_project_is_not_visible_until_bookkeeping_is_durable(
        self,
        database: Database,
        workspace: WorkspaceRoot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bookkeeping_started = threading.Event()
        release_bookkeeping = threading.Event()
        errors: list[BaseException] = []
        results: list[object] = []

        def pause_before_publication(
            workspace_arg: WorkspaceRoot,
            registry_arg: dict[str, ProjectEntry],
        ) -> None:
            bookkeeping_started.set()
            assert release_bookkeeping.wait(timeout=2)
            save_registry(workspace_arg, registry_arg)

        monkeypatch.setattr(
            "ferumind.core.project_writes.save_registry",
            pause_before_publication,
        )

        def create() -> None:
            other_conn = database.get_connection()
            try:
                results.append(
                    create_project(other_conn, workspace, key="not-visible", title="Not Visible")
                )
            except BaseException as exc:  # surfaced in the main test thread
                errors.append(exc)
            finally:
                other_conn.close()

        worker = threading.Thread(target=create)
        worker.start()
        assert bookkeeping_started.wait(timeout=2)
        assert (workspace / "projects/not-visible").is_dir()
        assert "not-visible" not in load_registry(workspace)
        with pytest.raises(ProjectNotFoundError):
            require_project(workspace, "not-visible")
        inspect_conn = database.get_connection()
        try:
            assert (
                inspect_conn.execute(
                    "SELECT COUNT(*) FROM operations "
                    "WHERE project_key = 'not-visible' AND operation_type = 'create_project'"
                ).fetchone()[0]
                == 1
            )
        finally:
            inspect_conn.close()

        release_bookkeeping.set()
        worker.join(timeout=3)

        assert not worker.is_alive()
        assert errors == []
        assert len(results) == 1
        assert "not-visible" in load_registry(workspace)

    def test_create_project_registry_failure_before_replace_compensates_hidden_state(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        before_snapshot_rows = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        before_operation_rows = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]

        def fail_before_replace(
            _workspace_arg: WorkspaceRoot,
            _registry_arg: dict[str, ProjectEntry],
        ) -> None:
            raise OSError("synthetic pre-replace registry failure")

        monkeypatch.setattr("ferumind.core.project_writes.save_registry", fail_before_replace)
        with pytest.raises(OSError, match="pre-replace"):
            create_project(conn, workspace, key="unpublished", title="Unpublished")

        assert "unpublished" not in load_registry(workspace)
        assert not (workspace / "projects/unpublished").exists()
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == before_snapshot_rows
        assert (
            conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == before_operation_rows
        )

    def test_create_project_accepts_post_replace_registry_fsync_failure(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []

        def save_then_fail(
            workspace_arg: WorkspaceRoot,
            registry_arg: dict[str, ProjectEntry],
        ) -> None:
            calls.append("save_registry")
            save_registry(workspace_arg, registry_arg)
            raise OSError("synthetic post-replace fsync failure")

        monkeypatch.setattr("ferumind.core.project_writes.save_registry", save_then_fail)
        result = create_project(conn, workspace, key="durable", title="Durable")

        # Every assertion below also holds when save_registry never fails, so
        # without this the test is vacuous the moment the patch stops biting.
        assert calls == ["save_registry"], "the injected fsync failure never fired"
        assert result.key == "durable"
        assert "durable" in load_registry(workspace)
        assert (workspace / "projects/durable/spine.md").is_file()
        operation = get_operation(conn, result.operation_id)
        assert operation is not None
        assert operation.snapshot_id == result.snapshot_id
