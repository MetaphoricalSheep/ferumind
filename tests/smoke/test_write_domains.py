"""A walk over every write domain milestone 01 extracted, over a real pipe.

What this proves that ``tests/integration/test_mcp_surface.py`` cannot: the
documented launcher launches, stdout carries protocol and nothing else, stdio
framing survives real payloads, and the process starts and stops cleanly. The
in-process suite is cheaper and stays; this one covers the process boundary.

Tests here share one server subprocess and therefore run in file order. That is
a deliberate trade — see ``conftest`` — and the sequencing is expressed through
session-scoped fixtures, so a single test selected on its own still gets the
state it needs.

To add a domain, see ``docs/smoke-harness.md``.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
from typing import cast

import pytest

from tests.smoke.conftest import SMOKE_PROJECT
from tests.smoke.session import Envelope, SmokeSession

pytestmark = pytest.mark.smoke

#: Distinguishes the per-test documents the patch domain creates.
_COUNTER = itertools.count(1)


# ── fixtures: the sequenced state later domains build on ───────────────────


@pytest.fixture(scope="session")
def project(session: SmokeSession) -> str:
    """The project every other domain writes into (``project_writes``)."""
    session.call("create_project", {"key": SMOKE_PROJECT, "title": "Smoke"}).require_ok()
    return SMOKE_PROJECT


@pytest.fixture(scope="session")
def canvas(session: SmokeSession, project: str) -> str:
    """A canvas document to patch, archive, and restore (``document_writes``)."""
    result = session.call(
        "create_document",
        {
            "project": project,
            "folder_path": "canvases",
            "title": "Smoke Canvas",
            "description": "Smoke-test canvas exercising the document write domain.",
            "content": "# Smoke Canvas\n\nOriginal body line.\n",
        },
    )
    return result.string("path")


# ── project_writes ─────────────────────────────────────────────────────────


def test_create_project_seeds_a_project_the_server_can_list(
    session: SmokeSession, project: str
) -> None:
    assert project in session.visible_projects()


# ── document_writes ────────────────────────────────────────────────────────


def test_create_document_saves_a_canvas(session: SmokeSession, canvas: str) -> None:
    assert canvas.startswith("canvases/")


def test_capture_note_saves_to_inbox(session: SmokeSession, project: str) -> None:
    result = session.call(
        "capture_note",
        {"project": project, "text": "A note captured over real stdio.", "title": "Smoke Note"},
    )
    data = result.require_ok()
    assert data["document_mutated"] is True
    assert result.string("path").startswith("inbox/")


def test_record_episode_appends_to_the_month_file(session: SmokeSession, project: str) -> None:
    result = session.call(
        "record_episode",
        {
            "project": project,
            "title": "Smoke harness ran",
            "summary": "The smoke harness drove the server over a real stdio pipe.",
        },
    )
    data = result.require_ok()
    assert data["document_mutated"] is True
    assert result.string("path").startswith("memory/episodes/")


# ── patch_writes: propose is not a save; apply is ──────────────────────────


ORIGINAL_LINE = "Original body line."
EDITED_LINE = "Edited by the smoke harness."


@pytest.fixture
def patch_target(session: SmokeSession, project: str) -> str:
    """A fresh document per test, so the patch tests do not depend on each other.

    An edit consumes the text it matched, so tests sharing one document would
    silently become order-dependent. Cheap to avoid: a document costs one call
    on a session that is already running.
    """
    result = session.call(
        "create_document",
        {
            "project": project,
            "folder_path": "canvases",
            "title": f"Smoke Patch Target {next(_COUNTER)}",
            "description": "Smoke-test canvas exercising the guarded patch domain.",
            "content": f"# Smoke Patch Target\n\n{ORIGINAL_LINE}\n",
        },
    )
    return result.string("path")


def _propose_body_edit(session: SmokeSession, project: str, path: str) -> Envelope:
    return session.call(
        "propose_exact_replace_patch",
        {
            "project": project,
            "path": path,
            "old_string": ORIGINAL_LINE,
            "new_string": EDITED_LINE,
        },
    )


def test_propose_stages_an_edit_without_writing(
    session: SmokeSession, project: str, patch_target: str
) -> None:
    data = _propose_body_edit(session, project, patch_target).require_ok()

    assert data["document_mutated"] is False
    assert data["requires_apply"] is True
    assert data["next_required_tool"] == "apply_patch"

    # The staged edit really did not reach disk.
    read = session.call("read_document", {"project": project, "path": patch_target}).require_ok()
    assert ORIGINAL_LINE in cast(str, read["content"])


def test_apply_patch_is_the_only_result_that_means_saved(
    session: SmokeSession, project: str, patch_target: str
) -> None:
    proposal = _propose_body_edit(session, project, patch_target)
    operation_id = proposal.string("operation_id")

    applied = session.call("apply_patch", {"project": project, "operation_id": operation_id})
    data = applied.require_ok()

    assert data["document_mutated"] is True
    assert data["operation_status"] == "applied"
    assert EDITED_LINE in cast(str, data["diff"])

    read = session.call("read_document", {"project": project, "path": patch_target}).require_ok()
    assert EDITED_LINE in cast(str, read["content"])


def test_a_spent_proposal_cannot_be_applied_twice(
    session: SmokeSession, project: str, patch_target: str
) -> None:
    """The failure arm travels over the wire as a machine-readable code."""
    proposal = _propose_body_edit(session, project, patch_target)
    operation_id = proposal.string("operation_id")
    session.call("apply_patch", {"project": project, "operation_id": operation_id}).require_ok()

    again = session.call("apply_patch", {"project": project, "operation_id": operation_id})

    assert again.ok is False
    assert again.error_code is not None


# ── upload_writes: direct and chunked ──────────────────────────────────────

_DIRECT_BYTES = b"smoke harness direct upload payload\n"
_CHUNKED_BYTES = b"smoke harness chunked upload payload, split across two chunks\n"


def test_direct_upload_stores_the_file_and_a_sidecar(session: SmokeSession, project: str) -> None:
    result = session.call(
        "upload_library_file",
        {
            "project": project,
            "filename": "smoke-direct.txt",
            "content_base64": base64.b64encode(_DIRECT_BYTES).decode("ascii"),
            "mime_type": "text/plain",
        },
    )
    data = result.require_ok()

    assert data["sha256"] == hashlib.sha256(_DIRECT_BYTES).hexdigest()
    assert data["size_bytes"] == len(_DIRECT_BYTES)
    assert result.string("path").startswith("library/")
    assert result.string("metadata_path").endswith(".json")


def test_chunked_upload_reassembles_across_the_pipe(session: SmokeSession, project: str) -> None:
    """Chunked upload is the one flow whose correctness depends on framing.

    Three round trips carrying base64 payloads, with a hash the server checks
    at finalize — so a byte lost or reordered in the stdio stream fails here
    rather than silently storing a corrupt file.
    """
    digest = hashlib.sha256(_CHUNKED_BYTES).hexdigest()
    midpoint = len(_CHUNKED_BYTES) // 2
    chunks = [_CHUNKED_BYTES[:midpoint], _CHUNKED_BYTES[midpoint:]]

    started = session.call(
        "start_library_file_upload",
        {
            "project": project,
            "filename": "smoke-chunked.txt",
            "total_size": len(_CHUNKED_BYTES),
            "total_chunks": len(chunks),
            "mime_type": "text/plain",
            "expected_sha256": digest,
        },
    )
    upload_id = started.string("upload_id")

    for index, chunk in enumerate(chunks):
        session.call(
            "append_upload_chunk",
            {
                "project": project,
                "upload_id": upload_id,
                "chunk_index": index,
                "chunk_base64": base64.b64encode(chunk).decode("ascii"),
            },
        ).require_ok()

    finalized = session.call(
        "finalize_library_file_upload", {"project": project, "upload_id": upload_id}
    )
    data = finalized.require_ok()

    assert data["sha256"] == digest
    assert data["size_bytes"] == len(_CHUNKED_BYTES)


# ── lifecycle_writes: archive, unarchive, restore ──────────────────────────


@pytest.fixture(scope="session")
def disposable_document(session: SmokeSession, project: str) -> str:
    """A document whose whole purpose is to be archived and brought back."""
    result = session.call(
        "create_document",
        {
            "project": project,
            "folder_path": "canvases",
            "title": "Smoke Lifecycle",
            "description": "Smoke-test canvas exercising archive and unarchive.",
            "content": "# Smoke Lifecycle\n\nArchive me.\n",
        },
    )
    return result.string("path")


def test_archive_then_unarchive_round_trips(
    session: SmokeSession, project: str, disposable_document: str
) -> None:
    """No hard delete: archiving moves the file and unarchiving brings it back."""
    archived = session.call("archive_document", {"project": project, "path": disposable_document})
    archived_path = archived.string("archived_path")
    assert archived_path.startswith("archive/")

    restored = session.call(
        "unarchive_document", {"project": project, "archived_path": archived_path}
    )
    assert restored.string("path") == disposable_document

    session.call("read_document", {"project": project, "path": disposable_document}).require_ok()


def test_restore_snapshot_reverses_an_applied_edit(
    session: SmokeSession, project: str, patch_target: str
) -> None:
    """Every mutation is snapshot-protected, and the restore is itself reversible.

    The snapshot restored here is the one ``apply_patch`` returned, which is
    the pre-edit state. A document's *creation* snapshot has no before-content
    to restore — there was no document — and asking for it is refused with
    ``SNAPSHOT_NOT_FOUND``.
    """
    proposal = _propose_body_edit(session, project, patch_target)
    applied = session.call(
        "apply_patch", {"project": project, "operation_id": proposal.string("operation_id")}
    )
    snapshot_id = applied.string("snapshot_id")

    listing = session.call(
        "list_snapshots", {"project": project, "path": patch_target}
    ).require_ok()
    snapshots = cast(list[dict[str, object]], listing["snapshots"])
    assert snapshot_id in {cast(str, entry["id"]) for entry in snapshots}

    restored = session.call("restore_snapshot", {"project": project, "snapshot_id": snapshot_id})
    data = restored.require_ok()

    assert data["document_mutated"] is True
    assert data["restored_from_snapshot_id"] == snapshot_id
    assert data["rollback_snapshot_id"], "a restore must leave a way back"

    read = session.call("read_document", {"project": project, "path": patch_target}).require_ok()
    assert ORIGINAL_LINE in cast(str, read["content"]), "the edit was not rolled back"


# ── the surface itself ─────────────────────────────────────────────────────


def test_the_whole_tool_surface_is_reachable_over_stdio(session: SmokeSession) -> None:
    """Registration is checked in-process; this proves it survives the transport."""
    names = session.tool_names()

    assert "apply_patch" in names
    assert "create_document" in names
    assert "upload_library_file" in names
    assert "archive_document" in names
    assert "create_project" in names
    assert len(names) >= 40, f"tool surface looks truncated: {sorted(names)}"


def test_an_unknown_project_is_a_machine_readable_refusal(session: SmokeSession) -> None:
    """The error arm has to survive the wire too, envelope and all."""
    result = session.call("get_context", {"project": "no-such-project"})

    result.require_error("PROJECT_NOT_FOUND")


def test_nothing_but_protocol_reached_stdout(session: SmokeSession) -> None:
    """The one rule that makes stdio work at all.

    There is no separate scan here, and that is the point: every line this
    session read from stdout was parsed as JSON-RPC on the way in, at DEBUG
    logging. Reaching this test means a full domain walk — dozens of calls,
    base64 payloads, error arms — produced no contamination. A stray ``print``
    would have failed the call that emitted it.

    What this adds is the other half: logging that loud must have gone
    *somewhere*, and stderr is the only place left.
    """
    assert session.stderr_text, "DEBUG logging produced nothing on stderr"
