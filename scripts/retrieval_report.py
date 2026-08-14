#!/usr/bin/env python3
"""Report and operator entry points for the retrieval harness (RET-01 / RET-06).

Assertion mode rides in ``pytest`` and needs nothing here. This script covers
the two things a human does by hand:

    uv run python scripts/retrieval_report.py              # print the table
    uv run python scripts/retrieval_report.py --update     # re-record the baseline
    uv run python scripts/retrieval_report.py --update --accept-corpus-change
    uv run python scripts/retrieval_report.py --operator --queries PATH

**Operator mode reads real user data**, so every guard it has fails closed:
the workspace comes from the existing configuration and cannot be passed as an
argument, the database is opened read-only at the SQLite level rather than by
convention, it refuses to run under ``CI``, it refuses to write a report into a
tracked path, and the report it writes carries aggregate counts only — never a
path, a query, or a snippet. An operator pasting a report into a ticket must not
thereby paste their workspace into it.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tests.retrieval.corpus import (  # noqa: E402
    CORPUS_ROOT,
    QUERIES_PATH,
    build_corpus_workspace,
    run_harness,
)
from tests.retrieval.labels import load_query_set  # noqa: E402
from tests.retrieval.operator import OperatorRefusedError, run_operator_mode  # noqa: E402
from tests.retrieval.ratchet import (  # noqa: E402
    BASELINE_PATH,
    CORPUS_ACCEPT_MESSAGE,
    UPDATE_COMMAND,
    baseline_from,
    compare,
    corpus_fingerprint,
    format_failure,
    load_baseline,
    update_baseline,
)
from tests.retrieval.report import render  # noqa: E402
from tests.retrieval.scorer import RunMetrics  # noqa: E402


def _measure_synthetic() -> tuple[str, int, RunMetrics]:
    query_set = load_query_set(QUERIES_PATH)
    with tempfile.TemporaryDirectory() as tmp:
        corpus = build_corpus_workspace(Path(tmp) / "workspace")
        try:
            metrics = run_harness(corpus, query_set)
            return sqlite3.sqlite_version, corpus.documents_indexed, metrics
        finally:
            corpus.close()


def report() -> int:
    sqlite_version, documents, metrics = _measure_synthetic()
    print(render(metrics, sqlite_version=sqlite_version, documents=documents))
    if BASELINE_PATH.is_file():
        current = baseline_from(
            metrics,
            sqlite_version=sqlite_version,
            corpus_sha256=corpus_fingerprint(CORPUS_ROOT, QUERIES_PATH),
        )
        result = compare(load_baseline(BASELINE_PATH), current)
        if not result.is_clean:
            print("\n" + format_failure(result, update_command=UPDATE_COMMAND))
            return 1
    return 0


def update(*, accept_corpus_change: bool = False) -> int:
    sqlite_version, _, metrics = _measure_synthetic()
    current = baseline_from(
        metrics,
        sqlite_version=sqlite_version,
        corpus_sha256=corpus_fingerprint(CORPUS_ROOT, QUERIES_PATH),
    )
    try:
        comparison = update_baseline(
            BASELINE_PATH,
            current,
            accept_corpus_change=accept_corpus_change,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Recorded {BASELINE_PATH.name} on SQLite {sqlite_version}.")
    for note in comparison.notes:
        if note == CORPUS_ACCEPT_MESSAGE or "not directly comparable" in note:
            print(note)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="Re-record the baseline.")
    parser.add_argument(
        "--accept-corpus-change",
        action="store_true",
        help=(
            "With --update: allow re-recording after an intentional corpus "
            "replacement. Refuses to launder an ordinary same-corpus regression."
        ),
    )
    parser.add_argument(
        "--operator",
        action="store_true",
        help="Run read-only against the configured live workspace.",
    )
    parser.add_argument("--queries", type=Path, help="Operator query file (must be Git-ignored).")
    parser.add_argument("--out", type=Path, help="Operator report path (must be Git-ignored).")
    args = parser.parse_args(argv)

    if args.accept_corpus_change and not args.update:
        parser.error("--accept-corpus-change requires --update")

    if args.operator:
        if args.queries is None:
            parser.error("--operator requires --queries")
        try:
            print(run_operator_mode(args.queries, args.out, ci=os.environ.get("CI")))
        except OperatorRefusedError as exc:
            print(f"Refused: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.update:
        return update(accept_corpus_change=args.accept_corpus_change)
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
