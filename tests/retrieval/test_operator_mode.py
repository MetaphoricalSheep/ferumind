"""Operator mode's guards, each shown refusing.

Operator mode is the only part of the harness that reads real user data. Its
guards are therefore the only ones whose failure would be a disclosure rather
than a wrong number, so every one is exercised in the direction that says no.

Nothing here touches a live workspace: the "live" workspace under test is a
throwaway built in ``tmp_path``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.retrieval.corpus import CorpusWorkspace, build_corpus_workspace
from tests.retrieval.operator import (
    REPO_ROOT,
    OperatorRefusedError,
    assert_not_ci,
    assert_path_is_not_tracked,
    open_readonly,
    run_operator_mode,
)


@pytest.fixture
def live_like(tmp_path: Path) -> CorpusWorkspace:
    """A throwaway workspace standing in for a real one."""
    return build_corpus_workspace(tmp_path / "workspace")


class TestCiRefusal:
    def test_refuses_when_ci_is_set(self) -> None:
        with pytest.raises(OperatorRefusedError, match="under CI"):
            assert_not_ci("true")

    def test_refuses_on_any_truthy_ci_value(self) -> None:
        """GitHub sets CI=true; other runners set other strings. Any value counts."""
        with pytest.raises(OperatorRefusedError):
            assert_not_ci("1")

    def test_allows_an_unset_ci(self) -> None:
        assert_not_ci(None)
        assert_not_ci("")


class TestTrackedPathRefusal:
    def test_refuses_a_tracked_file(self) -> None:
        """A tracked destination is one 'git add -u' away from being committed."""
        with pytest.raises(OperatorRefusedError, match="tracked by Git"):
            assert_path_is_not_tracked(REPO_ROOT / "pyproject.toml", role="report")

    def test_refuses_an_untracked_but_unignored_path_in_the_repo(self) -> None:
        """The likelier accident: it shows in git status and the next tidy-up commits it."""
        with pytest.raises(OperatorRefusedError, match="not Git-ignored"):
            assert_path_is_not_tracked(REPO_ROOT / "operator-report.txt", role="report")

    def test_allows_a_git_ignored_path_inside_the_repo(self) -> None:
        assert_path_is_not_tracked(REPO_ROOT / "htmlcov" / "report.txt", role="report")

    def test_allows_a_path_outside_the_repository(self, tmp_path: Path) -> None:
        assert_path_is_not_tracked(tmp_path / "queries.txt", role="query")

    def test_containment_is_not_a_string_prefix_check(self, tmp_path: Path) -> None:
        """A sibling directory sharing the repo's name prefix must not read as inside it."""
        decoy = tmp_path / (REPO_ROOT.name + "-notes")
        decoy.mkdir()
        assert_path_is_not_tracked(decoy / "queries.txt", role="query")


class TestReadOnly:
    def test_the_connection_cannot_write(self, live_like: CorpusWorkspace) -> None:
        """Enforced by SQLite, not by only calling read functions."""
        database = live_like.workspace / ".ferumind" / "ferumind.sqlite"
        live_like.close()
        connection = open_readonly(database)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("DELETE FROM documents")
        finally:
            connection.close()

    def test_a_missing_index_refuses_rather_than_creating_one(self, tmp_path: Path) -> None:
        """``sqlite3.connect`` would happily create an empty database; mode=ro must not."""
        with pytest.raises(OperatorRefusedError, match="no index"):
            open_readonly(tmp_path / "absent.sqlite")

    def test_running_changes_no_user_content_and_logs_nothing(
        self, live_like: CorpusWorkspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Measured, not assumed: no Markdown touched, no operation or snapshot recorded.

        The database is in WAL mode, and a WAL **reader** — even one opened
        ``mode=ro`` — creates the ``-shm`` and ``-wal`` sidecars it needs for the
        shared-memory index. That is SQLite's documented behaviour, not a write
        to user data, so those two files are excluded here deliberately rather
        than silently: the guarantee being made is about content, and pretending
        a read leaves literally zero filesystem trace would be a stronger claim
        than SQLite can honour.
        """
        workspace = live_like.workspace
        live_like.close()
        monkeypatch.setenv("FERUMIND_WORKSPACE", str(workspace))

        def content_state() -> dict[Path, tuple[int, int]]:
            return {
                path: (path.stat().st_mtime_ns, path.stat().st_size)
                for path in sorted(Path(workspace).rglob("*"))
                if path.is_file()
                and path.suffix not in {"-shm", "-wal"}
                and not path.name.endswith(("-shm", "-wal"))
            }

        before = content_state()
        operations_before = _count(workspace, "operations")
        snapshots_before = _count(workspace, "snapshots")

        queries = tmp_path / "queries.txt"
        queries.write_text("battery\nradio\n", encoding="utf-8")
        run_operator_mode(queries, None, ci=None)

        assert content_state() == before, "operator mode modified workspace content"
        assert _count(workspace, "operations") == operations_before
        assert _count(workspace, "snapshots") == snapshots_before

    def test_no_markdown_document_is_touched(
        self, live_like: CorpusWorkspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The claim that matters most, stated on its own so it cannot be diluted."""
        workspace = live_like.workspace
        live_like.close()
        monkeypatch.setenv("FERUMIND_WORKSPACE", str(workspace))

        before = {p: p.read_bytes() for p in sorted(Path(workspace).rglob("*.md"))}
        assert before, "fixture workspace has no documents, so this proves nothing"

        queries = tmp_path / "queries.txt"
        queries.write_text("battery\n", encoding="utf-8")
        run_operator_mode(queries, None, ci=None)

        assert {p: p.read_bytes() for p in sorted(Path(workspace).rglob("*.md"))} == before


class TestReportContents:
    def test_the_report_carries_no_document_or_query_text(
        self, live_like: CorpusWorkspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator pasting this into a ticket must not paste their workspace with it."""
        workspace = live_like.workspace
        live_like.close()
        monkeypatch.setenv("FERUMIND_WORKSPACE", str(workspace))

        queries = tmp_path / "queries.txt"
        queries.write_text("condensation\nAWS-07\n", encoding="utf-8")
        report = run_operator_mode(queries, tmp_path / "out.txt", ci=None)

        assert "condensation" not in report
        assert "AWS-07" not in report
        assert ".md" not in report
        assert str(workspace) not in report
        assert (tmp_path / "out.txt").read_text(encoding="utf-8").strip() == report.strip()

    def test_guards_run_before_anything_is_read(self, tmp_path: Path) -> None:
        """A refusal must happen before a workspace is opened, not after."""
        with pytest.raises(OperatorRefusedError, match="under CI"):
            run_operator_mode(tmp_path / "nonexistent.txt", None, ci="true")


def _count(workspace: Path, table: str) -> int:
    connection = sqlite3.connect(workspace / ".ferumind" / "ferumind.sqlite")
    try:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
        return int(row[0])
    finally:
        connection.close()
