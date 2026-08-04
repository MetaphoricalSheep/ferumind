"""Tests for the SQLite migration framework (spec-versioning §2.4)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lattice.db import database as database_module
from lattice.db.database import Database, MigrationError, discover_migrations


def _user_version(db: Database) -> int:
    conn = db.get_connection()
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def test_fresh_database_applies_schema_and_sets_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "lattice.sqlite")
    db.init_schema()
    conn = db.get_connection()
    try:
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert {"documents", "operations", "snapshots", "mcp_call_observations"} <= tables
    assert "sessions" not in tables
    assert "canvases" not in tables
    assert "projects" not in tables
    assert "document_blocks" not in tables
    assert "document_links" not in tables
    # No migrations shipped yet: baseline version 0.
    assert _user_version(db) == 0
    assert db.db_path.stat().st_mode & 0o777 == 0o600
    assert db.db_path.parent.stat().st_mode & 0o777 == 0o700


def test_fresh_database_jumps_to_latest_version(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_add_widgets.sql").write_text(
        "CREATE TABLE widgets (id TEXT PRIMARY KEY);", encoding="utf-8"
    )
    db = Database(tmp_path / "lattice.sqlite", migrations_dir=migrations)
    db.init_schema()
    # Fresh DBs get schema.sql + latest version; history is never replayed,
    # so the migration's table does not exist.
    assert _user_version(db) == 1
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='widgets'"
        ).fetchone()
    finally:
        conn.close()
    assert row is None


def test_failed_fresh_schema_is_rolled_back_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_schema = tmp_path / "bad-schema.sql"
    bad_schema.write_text(
        "CREATE TABLE partial (id TEXT PRIMARY KEY); SYNTAX ERROR;",
        encoding="utf-8",
    )
    db = Database(tmp_path / "lattice.sqlite")
    real_schema = database_module.SCHEMA_PATH
    monkeypatch.setattr(database_module, "SCHEMA_PATH", bad_schema)

    with pytest.raises(MigrationError, match="initialization failed"):
        db.init_schema()

    conn = db.get_connection()
    try:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='partial'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()

    monkeypatch.setattr(database_module, "SCHEMA_PATH", real_schema)
    db.init_schema()
    conn = db.get_connection()
    try:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='operations'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def test_existing_database_applies_pending_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "lattice.sqlite"
    Database(db_path).init_schema()  # baseline at version 0

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_add_widgets.sql").write_text(
        "CREATE TABLE widgets (id TEXT PRIMARY KEY);", encoding="utf-8"
    )
    (migrations / "0002_add_gadgets.sql").write_text(
        "CREATE TABLE gadgets (id TEXT PRIMARY KEY);", encoding="utf-8"
    )
    db = Database(db_path, migrations_dir=migrations)
    db.init_schema()
    assert _user_version(db) == 2
    conn = db.get_connection()
    try:
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert {"widgets", "gadgets"} <= tables


def test_migrations_are_idempotent_across_restarts(tmp_path: Path) -> None:
    db_path = tmp_path / "lattice.sqlite"
    Database(db_path).init_schema()
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_add_widgets.sql").write_text(
        "CREATE TABLE widgets (id TEXT PRIMARY KEY);", encoding="utf-8"
    )
    db = Database(db_path, migrations_dir=migrations)
    db.init_schema()
    db.init_schema()  # second startup: nothing to apply
    assert _user_version(db) == 1


def test_failed_migration_rolls_back_and_keeps_version(tmp_path: Path) -> None:
    db_path = tmp_path / "lattice.sqlite"
    Database(db_path).init_schema()
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_bad.sql").write_text(
        "CREATE TABLE widgets (id TEXT PRIMARY KEY); SYNTAX ERROR;", encoding="utf-8"
    )
    db = Database(db_path, migrations_dir=migrations)
    with pytest.raises(MigrationError):
        db.init_schema()
    assert _user_version(db) == 0
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='widgets'"
        ).fetchone()
    finally:
        conn.close()
    assert row is None


def test_newer_database_is_refused(tmp_path: Path) -> None:
    db_path = tmp_path / "lattice.sqlite"
    db = Database(db_path)
    db.init_schema()
    conn = db.get_connection()
    try:
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(MigrationError, match="newer"):
        db.init_schema()


def _make_v1_database(db_path: Path) -> None:
    """Create a database shaped like the pre-versioning v1 schema."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY);
            CREATE TABLE documents (project_key TEXT, path TEXT, title TEXT);
            CREATE TABLE mcp_call_observations (
                id TEXT PRIMARY KEY, session_id TEXT, tool_name TEXT
            );
            INSERT INTO sessions VALUES ('sess-1');
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_v1_database_is_sidelined_and_rebuilt(tmp_path: Path) -> None:
    db_path = tmp_path / "lattice.sqlite"
    _make_v1_database(db_path)

    db = Database(db_path)
    db.init_schema()

    conn = db.get_connection()
    try:
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        obs_cols = {row[1] for row in conn.execute("PRAGMA table_info(mcp_call_observations)")}
    finally:
        conn.close()
    assert "sessions" not in tables
    assert "correlation_id" in obs_cols
    # The v1 file is preserved next to the new one, never deleted.
    backup = tmp_path / "lattice-v1-backup.sqlite"
    assert backup.is_file()
    assert backup.stat().st_mode & 0o777 == 0o600
    old = sqlite3.connect(str(backup))
    try:
        assert old.execute("SELECT id FROM sessions").fetchone() == ("sess-1",)
    finally:
        old.close()


def test_legacy_sideline_never_overwrites_an_existing_backup(tmp_path: Path) -> None:
    (tmp_path / "lattice-v1-backup.sqlite").write_text("keep me", encoding="utf-8")
    db_path = tmp_path / "lattice.sqlite"
    _make_v1_database(db_path)
    Database(db_path).init_schema()
    assert (tmp_path / "lattice-v1-backup.sqlite").read_text(encoding="utf-8") == "keep me"
    assert (tmp_path / "lattice-v1-backup-2.sqlite").is_file()


def test_v2_baseline_at_version_zero_is_not_mistaken_for_legacy(tmp_path: Path) -> None:
    db_path = tmp_path / "lattice.sqlite"
    Database(db_path).init_schema()
    conn = Database(db_path).get_connection()
    try:
        conn.execute(
            "INSERT INTO snapshots (id, project_key, snapshot_dir, reason, created_at)"
            " VALUES ('s', 'p', '/tmp/s', 'apply_patch', 't')"
        )
        conn.commit()
    finally:
        conn.close()
    Database(db_path).init_schema()  # restart: must keep the row, not sideline
    conn = Database(db_path).get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    finally:
        conn.close()
    assert not (tmp_path / "lattice-v1-backup.sqlite").exists()


def test_discover_migrations_rejects_gaps(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0002_skipped.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="contiguous"):
        discover_migrations(migrations)


def test_discover_migrations_rejects_bad_names(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "1_bad-name.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="NNNN_slug"):
        discover_migrations(migrations)


def test_discover_migrations_empty_or_missing_dir(tmp_path: Path) -> None:
    assert discover_migrations(tmp_path / "nope") == []
    empty = tmp_path / "migrations"
    empty.mkdir()
    assert discover_migrations(empty) == []
