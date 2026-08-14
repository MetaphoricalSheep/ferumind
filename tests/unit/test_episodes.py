"""Tests for ``record_episode``: the episodic-memory write family.

Kept in its own module rather than folded into ``test_writes.py``: episodes
are one cohesive write family, and ``core/writes.py`` is a god module already
slated for extraction, so its tests are worth splitting along the same seams.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ferumind.core import document_writes as document_writes_module
from ferumind.core import project_writes as project_writes_module
from ferumind.core.document_map import build_document_map
from ferumind.core.document_writes import EpisodeDraft, record_episode
from ferumind.core.edit_targets import find_in_document
from ferumind.core.errors import DocumentArchivedError, ValidationError, WorkspaceMismatchError
from ferumind.core.frontmatter import parse_frontmatter, validate_description
from ferumind.core.lifecycle_writes import (
    archive_document,
    restore_snapshot,
    unarchive_document,
)
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.search import search_project
from ferumind.core.snapshots import find_snapshot_dir
from ferumind.core.write_limits import (
    MAX_EPISODE_RELATED_PATHS,
    MAX_EPISODE_SUMMARY_CHARS,
    MAX_EPISODE_TITLE_CHARS,
)

MONTH_PATH = "memory/episodes/2026-08.md"


def _freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """Pin server time. The clock must be patchable or the rollover test is
    only deterministic during one calendar month."""
    monkeypatch.setattr(document_writes_module, "_episode_now", lambda: moment)


def _record(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    title: str = "Something happened",
    summary: str = "What happened, and what was decided at the time.",
    **kwargs: object,
) -> document_writes_module.EpisodeResult:
    draft = EpisodeDraft(title=title, summary=summary, **kwargs)  # pyright: ignore[reportArgumentType]
    return record_episode(conn, workspace, project, draft=draft)


def _month_file(workspace: WorkspaceRoot, project: str, path: str = MONTH_PATH) -> Path:
    return workspace / "projects" / project / path


def _explode(*_args: object, **_kwargs: object) -> None:
    """Fail the write after its snapshot exists, so rollback has work to do."""
    raise RuntimeError("induced failure after the snapshot was created")


class TestFirstEpisode:
    def test_first_episode_creates_the_folder_and_month_file(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        episodes_dir = workspace / "projects" / project / "memory" / "episodes"
        assert not episodes_dir.exists(), "nothing may seed the folder before the first record"

        result = _record(conn, workspace, project)

        assert result.month_file_created is True
        assert result.path == f"memory/episodes/{datetime.now(UTC):%Y-%m}.md"
        assert episodes_dir.is_dir()

    def test_the_month_file_is_an_append_ledger_in_the_memory_folder(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        result = _record(conn, workspace, project)
        content = _month_file(workspace, project, result.path).read_text(encoding="utf-8")
        fm = parse_frontmatter(content)

        # memory/ defaults to free; a ledger nobody rewrites needs append.
        assert fm["edit_policy"] == "append"
        assert fm["project"] == project
        assert validate_description(fm["description"]) == fm["description"]
        assert "episode ledger" in str(fm["description"]).lower()
        assert result.folder == "memory"

    def test_the_episode_is_an_addressable_dated_section(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        result = _record(conn, workspace, project, title="Bench session dropped")
        content = _month_file(workspace, project, result.path).read_text(encoding="utf-8")

        assert f"## {datetime.now(UTC):%Y-%m-%d} — Bench session dropped" in content
        assert result.episode_id in content

    def test_absent_fields_emit_no_empty_lines(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        result = _record(conn, workspace, project)
        content = _month_file(workspace, project, result.path).read_text(encoding="utf-8")

        assert "Related:" not in content
        assert "Follows:" not in content


class TestAppending:
    def test_a_second_episode_leaves_the_first_bytes_untouched(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        first = _record(conn, workspace, project, title="First")
        path = _month_file(workspace, project, first.path)
        before = path.read_text(encoding="utf-8")

        second = _record(conn, workspace, project, title="Second")
        after = path.read_text(encoding="utf-8")

        assert second.month_file_created is False
        assert second.path == first.path
        # Byte-for-byte on the prefix, not "the file got longer".
        assert after.startswith(before.rstrip())
        assert first.episode_id in after
        assert second.episode_id in after

    def test_ids_are_unique_and_stable_across_many_records(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        ids = [_record(conn, workspace, project, title=f"Episode {n}").episode_id for n in range(8)]

        assert len(set(ids)) == 8
        content = _month_file(workspace, project).read_text(encoding="utf-8")
        # A later append renumbers nothing: every id is still present, in order.
        positions = [content.index(episode_id) for episode_id in ids]
        assert positions == sorted(positions)

    def test_a_new_month_starts_a_new_file_and_leaves_the_old_one_alone(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _freeze(monkeypatch, datetime(2026, 8, 31, 23, 30, tzinfo=UTC))
        august = _record(conn, workspace, project, title="Last of August")
        august_file = _month_file(workspace, project, august.path)
        august_bytes = august_file.read_bytes()

        _freeze(monkeypatch, datetime(2026, 9, 1, 0, 30, tzinfo=UTC))
        september = _record(conn, workspace, project, title="First of September")

        assert august.path == "memory/episodes/2026-08.md"
        assert september.path == "memory/episodes/2026-09.md"
        assert september.month_file_created is True
        assert august_file.read_bytes() == august_bytes

    def test_the_append_lands_after_an_out_of_band_edit(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        first = _record(conn, workspace, project, title="First")
        path = _month_file(workspace, project, first.path)
        edited = path.read_text(encoding="utf-8") + "\nHand-written by the user in vim.\n"
        path.write_text(edited, encoding="utf-8")

        second = _record(conn, workspace, project, title="Second")
        final = path.read_text(encoding="utf-8")

        assert "Hand-written by the user in vim." in final
        assert second.episode_id in final
        sources = {
            row[0]
            for row in conn.execute(
                "SELECT source FROM operations WHERE project_key = ? AND target_path = ?",
                (project, first.path),
            ).fetchall()
        }
        assert "out-of-band" in sources

    def test_concurrent_records_both_land_and_the_file_stays_parseable(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        _record(conn, workspace, project, title="Seed")
        recorded: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker(label: str) -> None:
            own = sqlite3.connect(workspace / ".ferumind" / "ferumind.sqlite")
            own.row_factory = sqlite3.Row
            try:
                barrier.wait(timeout=10)
                result = _record(own, workspace, project, title=label)
                recorded.append(result.episode_id)
            except BaseException as exc:
                errors.append(exc)
            finally:
                own.close()

        threads = [threading.Thread(target=worker, args=(f"Concurrent {n}",)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        content = _month_file(workspace, project).read_text(encoding="utf-8")
        assert len(recorded) == 2
        for episode_id in recorded:
            assert episode_id in content
        assert parse_frontmatter(content)["project"] == project


class TestServerAuthority:
    def test_the_date_comes_from_the_server_not_the_arguments(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _freeze(monkeypatch, datetime(2026, 8, 8, 12, 0, tzinfo=UTC))

        result = _record(
            conn,
            workspace,
            project,
            title="Recorded on 2019-01-01",
            summary="The model believes today is 1999-12-31 and says so here.",
        )
        content = _month_file(workspace, project, result.path).read_text(encoding="utf-8")

        assert result.path == "memory/episodes/2026-08.md"
        assert "## 2026-08-08 — Recorded on 2019-01-01" in content
        assert "## 1999-12-31" not in content

    def test_ids_are_server_generated_in_the_documented_shape(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        result = _record(conn, workspace, project)
        # Asserted against the shape, not the module's private pattern: this is
        # the id a follow-up quotes verbatim, so its form is a contract.
        assert re.fullmatch(r"ep_[0-9a-f]{12}", result.episode_id)


class TestFollowUps:
    def test_a_follow_up_links_forward_and_is_found_by_exact_search(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        original = _record(conn, workspace, project, title="Pain after bench")
        follow_up = _record(
            conn,
            workspace,
            project,
            title="Pain recurred",
            related_episode_id=original.episode_id,
        )
        content = _month_file(workspace, project).read_text(encoding="utf-8")

        assert f"- Follows: {original.episode_id}" in content
        found = find_in_document(
            content=content,
            project_key=project,
            path=follow_up.path,
            query=original.episode_id,
        )
        assert len(found.matches) >= 2  # the original's own ID line, and the follow-up's

    def test_a_dangling_reference_is_accepted(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        """The target may live in an archived month file; resolving it would
        mean a workspace-wide scan on every write."""
        result = _record(conn, workspace, project, related_episode_id="ep_0123456789ab")
        content = _month_file(workspace, project, result.path).read_text(encoding="utf-8")
        assert "- Follows: ep_0123456789ab" in content

    @pytest.mark.parametrize(
        "bad_id", ["not-an-id", "ep_short", "doc_0123456789ab", "ep_0123456789AB", ""]
    )
    def test_a_malformed_reference_is_rejected(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, bad_id: str
    ) -> None:
        with pytest.raises(ValidationError):
            _record(conn, workspace, project, related_episode_id=bad_id)


class TestValidation:
    @pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
    def test_blank_title_or_summary_is_refused(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, blank: str
    ) -> None:
        with pytest.raises(ValidationError):
            _record(conn, workspace, project, title=blank)
        with pytest.raises(ValidationError):
            _record(conn, workspace, project, summary=blank)

    def test_oversized_title_and_summary_are_bounded(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(ValidationError):
            _record(conn, workspace, project, title="t" * (MAX_EPISODE_TITLE_CHARS + 1))
        with pytest.raises(ValidationError):
            _record(conn, workspace, project, summary="s" * (MAX_EPISODE_SUMMARY_CHARS + 1))

    def test_too_many_related_paths_are_bounded(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        too_many = tuple(f"canvases/doc-{n}.md" for n in range(MAX_EPISODE_RELATED_PATHS + 1))
        with pytest.raises(ValidationError):
            _record(conn, workspace, project, related_paths=too_many)

    def test_nothing_is_written_when_validation_fails(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(ValidationError):
            _record(conn, workspace, project, title="  ")
        assert not (workspace / "projects" / project / "memory" / "episodes").exists()


class TestPathSafety:
    @pytest.mark.parametrize(
        "escape",
        [
            "../../etc/passwd",
            "../other-project/memory/secret.md",
            "/etc/passwd",
            "canvases/../../../outside.md",
        ],
    )
    def test_related_paths_cannot_escape_the_project(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, escape: str
    ) -> None:
        with pytest.raises((WorkspaceMismatchError, ValidationError)):
            _record(conn, workspace, project, related_paths=(escape,))

    def test_a_symlink_out_of_the_project_is_refused(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside-the-workspace.md"
        outside.write_text("not yours\n", encoding="utf-8")
        link = workspace / "projects" / project / "canvases" / "escape.md"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)

        with pytest.raises((WorkspaceMismatchError, ValidationError)):
            _record(conn, workspace, project, related_paths=("canvases/escape.md",))

    def test_a_path_in_another_project_is_refused(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        project_writes_module.create_project(conn, workspace, key="neighbour", title="Neighbour")
        with pytest.raises((WorkspaceMismatchError, ValidationError)):
            _record(
                conn,
                workspace,
                project,
                related_paths=("../neighbour/memory/private.md",),
            )

    def test_a_legitimate_related_path_survives_and_is_recorded(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        result = _record(conn, workspace, project, related_paths=("canvases/logs/2026-08.md",))
        content = _month_file(workspace, project, result.path).read_text(encoding="utf-8")
        assert "- Related: canvases/logs/2026-08.md" in content


class TestAuditTrail:
    @pytest.mark.parametrize("call_index", [0, 1])
    def test_both_create_and_append_are_snapshotted_and_logged(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, call_index: int
    ) -> None:
        results = [_record(conn, workspace, project, title=f"Episode {n}") for n in range(2)]
        result = results[call_index]

        assert result.snapshot_id is not None
        assert find_snapshot_dir(workspace / "projects" / project, result.snapshot_id) is not None
        row = conn.execute(
            "SELECT operation_type, target_path FROM operations WHERE id = ?",
            (result.operation_id,),
        ).fetchone()
        assert row["operation_type"] == "record_episode"
        assert row["target_path"] == result.path

    def test_the_operation_row_never_carries_the_episode_body(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        secret = "the-body-that-must-not-be-logged"
        result = _record(
            conn,
            workspace,
            project,
            summary=secret,
            related_paths=("canvases/private-plan.md",),
        )
        request_json = conn.execute(
            "SELECT request_json FROM operations WHERE id = ?", (result.operation_id,)
        ).fetchone()[0]

        assert secret not in request_json
        assert "private-plan" not in request_json
        assert "summary_bytes" in request_json
        assert "related_paths_count" in request_json

    def test_a_snapshot_round_trips_the_month_file(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        first = _record(conn, workspace, project, title="First")
        path = _month_file(workspace, project, first.path)
        one_episode = path.read_text(encoding="utf-8")

        second = _record(conn, workspace, project, title="Second")
        assert second.snapshot_id is not None
        restore_snapshot(conn, workspace, project, second.snapshot_id)

        assert path.read_text(encoding="utf-8") == one_episode


class TestRollback:
    def test_a_failure_after_the_snapshot_leaves_no_trace(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        before_rows = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
        # REL-025: the episode write runs in ``document_writes`` now. Patching
        # ``writes`` would still succeed — it keeps its own live
        # ``record_snapshot_in_db`` for uploads — and silently do nothing.
        monkeypatch.setattr(document_writes_module, "record_snapshot_in_db", _explode)

        with pytest.raises(RuntimeError):
            _record(conn, workspace, project)

        project_dir = workspace / "projects" / project
        assert not (project_dir / MONTH_PATH).exists()
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == before_rows
        snapshot_root = project_dir / ".ferumind" / "snapshots"
        orphans = list(snapshot_root.rglob("*")) if snapshot_root.exists() else []
        assert not [entry for entry in orphans if entry.is_dir() and entry.name.startswith("snap")]

    def test_an_append_failure_restores_the_previous_content(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = _record(conn, workspace, project, title="Survivor")
        path = _month_file(workspace, project, first.path)
        before = path.read_text(encoding="utf-8")
        # REL-025: the episode write runs in ``document_writes`` now. Patching
        # ``writes`` would still succeed — it keeps its own live
        # ``record_snapshot_in_db`` for uploads — and silently do nothing.
        monkeypatch.setattr(document_writes_module, "record_snapshot_in_db", _explode)

        with pytest.raises(RuntimeError):
            _record(conn, workspace, project, title="Never lands")

        assert path.read_text(encoding="utf-8") == before


class TestArchivedMonth:
    def test_an_archived_month_refuses_rather_than_stranding_history(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        first = _record(conn, workspace, project, title="Before the archive")
        archive_document(conn, workspace, project, path=first.path)
        live = _month_file(workspace, project, first.path)
        assert not live.exists()

        with pytest.raises(DocumentArchivedError) as caught:
            _record(conn, workspace, project, title="After the archive")

        details = caught.value.details
        assert details is not None, "the agent needs the archived path to recover"
        assert details["archived_path"] == f"archive/{first.path}"
        assert not live.exists(), "no fresh live file may be created over archived history"

        # The archived history is still recoverable, which is the whole point.
        unarchive_document(conn, workspace, project, archived_path=f"archive/{first.path}")
        assert first.episode_id in live.read_text(encoding="utf-8")


class TestRetrievalOnDayOne:
    def test_each_episode_is_its_own_addressable_section(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        titles = ["Bench dropped", "Pain recurred", "Technique changed"]
        for title in titles:
            _record(conn, workspace, project, title=title)
        content = _month_file(workspace, project).read_text(encoding="utf-8")

        document_map = build_document_map(content=content, project_key=project, path=MONTH_PATH)
        headings = [
            section.heading_text
            for section in document_map.sections
            if section.level == 2 and section.heading_text is not None
        ]
        assert len(headings) == len(titles)
        for title in titles:
            assert any(title in heading for heading in headings)

    def test_search_finds_episodes_in_the_memory_folder_with_no_code_change(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        _record(
            conn,
            workspace,
            project,
            title="Rotator cuff flare",
            summary="The overhead press was dropped after a rotatorcuffflare.",
        )
        results = search_project(conn, project, "rotatorcuffflare", folder="memory")
        assert results
        assert {result.path for result in results} == {MONTH_PATH}
