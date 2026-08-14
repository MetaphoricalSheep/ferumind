"""Operator mode: run the harness against a real workspace, read-only.

Useful, and a data-exfiltration path if built carelessly. Every guard here fails
closed, and each exists because the careless version is the obvious one:

* **No workspace argument.** The path comes from ``FERUMIND_WORKSPACE`` through
  the existing config. A ``--workspace`` flag would be a way to point this at
  any directory on the machine and write a report about it.
* **Read-only at the SQLite level**, via a ``file:…?mode=ro`` URI, not by
  discipline. A stale index is *reported*, never reconciled — reconciling would
  write, and "we only call read functions" is a promise, not a guarantee.
* **Refuses under ``CI``**, following the precedent ``scripts/tunnel.sh`` sets
  for operator-initiated actions.
* **Refuses a query or output path that Git tracks.** Queries disclose what a
  workspace is about as surely as documents do, so they stay out of the tree —
  checked with ``git check-ignore``, not documented and hoped for.
* **Aggregates only in the written report.** No path, no query text, no snippet,
  no title. An operator pasting a report into a ticket must not thereby paste
  their workspace into it.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess  # git plumbing only; fixed argv, never shell=True
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent.parent


class OperatorRefusedError(RuntimeError):
    """A guard said no. Never downgraded to a warning."""


def assert_not_ci(ci: str | None) -> None:
    """Operator mode is operator-initiated, by definition.

    Mirrors ``scripts/tunnel.sh``: an action that reads a human's private data
    should never be reachable from an automated run.
    """
    if ci:
        msg = "refusing to run operator mode under CI (CI is set)"
        raise OperatorRefusedError(msg)


def assert_path_is_not_tracked(path: Path, *, role: str) -> None:
    """Refuse a path Git tracks, or that would land in the tree untracked.

    Two separate failures matter. A **tracked** path means committing the file
    is one ``git add -u`` away. An **untracked but not ignored** path inside the
    repository is worse in practice: it shows up in ``git status`` as something
    to add, and the next person to tidy up commits it.
    """
    resolved = path.resolve()
    if not _is_inside(resolved, REPO_ROOT):
        return

    if _git_says_tracked(resolved):
        msg = (
            f"{role} path is tracked by Git ({path}); queries and reports must stay out of the tree"
        )
        raise OperatorRefusedError(msg)

    if not _git_says_ignored(resolved):
        msg = (
            f"{role} path is inside the repository and not Git-ignored ({path}); "
            "put it outside the checkout or add it to .gitignore first"
        )
        raise OperatorRefusedError(msg)


def open_readonly(database: Path) -> sqlite3.Connection:
    """Open the live index read-only, enforced by SQLite rather than by care."""
    if not database.is_file():
        msg = f"no index at {database}; run 'ferumind index' against the workspace first"
        raise OperatorRefusedError(msg)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _is_inside(path: Path, root: Path) -> bool:
    """Containment by path semantics, never by string prefix."""
    return path == root or root in path.parents


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        msg = "git is not available, so the tracked-path guard cannot run; refusing"
        raise OperatorRefusedError(msg)
    return subprocess.run([git, *args], cwd=cwd, capture_output=True, text=True, check=False)


def _git_says_tracked(path: Path) -> bool:
    return _git(["ls-files", "--error-unmatch", str(path)], cwd=REPO_ROOT).returncode == 0


def _git_says_ignored(path: Path) -> bool:
    return _git(["check-ignore", "-q", str(path)], cwd=REPO_ROOT).returncode == 0


def run_operator_mode(
    queries: Path,
    out: Path | None,
    *,
    ci: str | None = None,
) -> str:
    """Guard, then measure the configured workspace read-only.

    Returns the aggregate report as text. Writing it is the caller's business
    only after :func:`assert_path_is_not_tracked` has cleared the destination.
    """
    assert_not_ci(ci)
    assert_path_is_not_tracked(queries, role="query")
    if out is not None:
        assert_path_is_not_tracked(out, role="report")
    if not queries.is_file():
        msg = f"query file not found: {queries}"
        raise OperatorRefusedError(msg)

    workspace = _configured_workspace()
    connection = open_readonly(workspace / ".ferumind" / "ferumind.sqlite")
    try:
        report = _aggregate_report(connection, queries)
    finally:
        connection.close()

    if out is not None:
        out.write_text(report + "\n", encoding="utf-8")
    return report


def _configured_workspace() -> Path:
    """The workspace the rest of Ferumind would use. No override, by design."""
    from ferumind.core.config import load_config

    return Path(load_config().workspace_path)


def _aggregate_report(connection: sqlite3.Connection, queries: Path) -> str:
    """Counts only. Nothing here may carry document or query text.

    The report is deliberately dull: how many queries ran, how many returned
    anything, and the top-k counts. Adding "which document won" would make it a
    disclosure channel, and the whole point is that this file is safe to paste
    into a ticket.
    """
    from ferumind.core.search import search_project
    from tests.retrieval.scorer import RetrievedResult, rank_deterministically

    lines = [line.strip() for line in queries.read_text(encoding="utf-8").splitlines()]
    wanted = [line for line in lines if line and not line.startswith("#")]

    projects = [
        str(row["project_key"])
        for row in connection.execute(
            "SELECT DISTINCT project_key FROM documents ORDER BY project_key"
        )
    ]
    documents = int(next(iter(connection.execute("SELECT COUNT(*) AS n FROM documents")))["n"])

    ran = 0
    returned_any = 0
    total_hits = 0
    for query in wanted:
        for project in projects:
            results = rank_deterministically(
                tuple(
                    RetrievedResult(path=row.path, snippet=row.snippet, score=row.score)
                    for row in search_project(connection, project, query, limit=10)
                )
            )
            ran += 1
            if results:
                returned_any += 1
                total_hits += len(results)

    return "\n".join(
        (
            "Ferumind operator retrieval report (aggregates only)",
            f"  sqlite:            {sqlite3.sqlite_version}",
            f"  projects scanned:  {len(projects)}",
            f"  documents indexed: {documents}",
            f"  queries supplied:  {len(wanted)}",
            f"  searches run:      {ran}",
            f"  searches with >=1 result: {returned_any}",
            f"  total results returned:   {total_hits}",
            "",
            "No document paths, titles, snippets or query text are recorded here.",
        )
    )
