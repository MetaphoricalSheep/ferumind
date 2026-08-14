"""Tests for the SQLite migration framework (spec-versioning §2.4)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ferumind.db import database as database_module
from ferumind.db.database import Database, MigrationError, discover_migrations


def _user_version(db: Database) -> int:
    conn = db.get_connection()
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def test_fresh_database_applies_schema_and_sets_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "ferumind.sqlite")
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
    assert _user_version(db) == 3
    assert db.db_path.stat().st_mode & 0o777 == 0o600
    assert db.db_path.parent.stat().st_mode & 0o777 == 0o700

    conn = db.get_connection()
    try:
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        fts_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'section_index%'"
            )
        }
    finally:
        conn.close()
    assert "idx_mcp_call_observations_correlation_id" in indexes
    assert "section_index" in fts_tables


def test_fresh_database_jumps_to_latest_version(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_add_widgets.sql").write_text(
        "CREATE TABLE widgets (id TEXT PRIMARY KEY);", encoding="utf-8"
    )
    db = Database(tmp_path / "ferumind.sqlite", migrations_dir=migrations)
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
    db = Database(tmp_path / "ferumind.sqlite")
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
    db_path = tmp_path / "ferumind.sqlite"
    initial_migrations = tmp_path / "initial-migrations"
    initial_migrations.mkdir()
    Database(db_path, migrations_dir=initial_migrations).init_schema()

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
    db_path = tmp_path / "ferumind.sqlite"
    initial_migrations = tmp_path / "initial-migrations"
    initial_migrations.mkdir()
    Database(db_path, migrations_dir=initial_migrations).init_schema()
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
    db_path = tmp_path / "ferumind.sqlite"
    initial_migrations = tmp_path / "initial-migrations"
    initial_migrations.mkdir()
    Database(db_path, migrations_dir=initial_migrations).init_schema()
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
    db_path = tmp_path / "ferumind.sqlite"
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


def test_populated_database_at_version_zero_migrates_without_data_loss(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ferumind.sqlite"
    schema_v0 = _historical_schema(tmp_path / "schema_v0.sql", schema_version=0)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(schema_v0.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO snapshots (id, project_key, snapshot_dir, reason, created_at)"
            " VALUES ('s', 'p', '/tmp/s', 'apply_patch', 't')"
        )
        conn.commit()
    finally:
        conn.close()

    Database(db_path).init_schema()  # restart

    conn = Database(db_path).get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    finally:
        conn.close()
    assert _user_version(Database(db_path)) == 3
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".sqlite") == [
        "ferumind.sqlite"
    ]


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


def test_readonly_connection_refuses_writes_and_missing_database(tmp_path: Path) -> None:
    missing = Database(tmp_path / "missing.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        missing.get_readonly_connection()
    assert not missing.db_path.exists()

    db = Database(tmp_path / "ferumind.sqlite")
    db.init_schema()
    before_version = _user_version(db)
    conn = db.get_readonly_connection()
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(
                "INSERT INTO snapshots (id, project_key, snapshot_dir, reason, created_at)"
                " VALUES ('s', 'p', 'd', 'test', 't')"
            )
    finally:
        conn.close()
    assert _user_version(db) == before_version


def test_correlation_index_migration_is_nonunique(tmp_path: Path) -> None:
    db_path = tmp_path / "ferumind.sqlite"
    schema_v0 = _historical_schema(tmp_path / "schema_v0.sql", schema_version=0)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(schema_v0.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO mcp_call_observations "
            "(id, correlation_id, tool_name, created_at, server_boot_id, process_id) "
            "VALUES ('one', 'same', 'x', '2026-01-01T00:00:00+00:00', 'b', 1), "
            "('two', 'same', 'y', '2026-01-02T00:00:00+00:00', 'b', 1)"
        )
        conn.commit()
    finally:
        conn.close()

    Database(db_path).init_schema()
    conn = Database(db_path).get_connection()
    try:
        index = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_mcp_call_observations_correlation_id'"
        ).fetchone()
        assert index is not None
        assert "UNIQUE" not in index["sql"].upper()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM mcp_call_observations WHERE correlation_id = 'same'"
            ).fetchone()[0]
            == 2
        )
    finally:
        conn.close()


def _historical_schema(target: Path, *, schema_version: int) -> Path:
    """Reconstruct a historical baseline by removing later additions.

    The alternative — committing frozen copies of every past schema — drifts
    the moment someone edits the current one, and the whole point of these
    tests is that fresh and migrated databases converge on the *current*
    definition. Undoing each numbered migration's addition keeps that honest.
    """
    assert schema_version in {0, 1, 2}
    text = database_module.SCHEMA_PATH.read_text(encoding="utf-8")
    if schema_version < 3:
        description = "    description TEXT NOT NULL DEFAULT '',\n"
        assert text.count(description) == 1
        text = text.replace(description, "")
    if schema_version < 2:
        # 0002 added the section_index FTS5 mirror.
        start = text.index("-- Section-level FTS5 mirror:")
        end = text.index("-- ── Durable system history")
        text = text[:start] + text[end:]
    if schema_version < 1:
        # 0001 added the non-unique observation correlation-id index.
        correlation_index = (
            "CREATE INDEX IF NOT EXISTS idx_mcp_call_observations_correlation_id\n"
            "    ON mcp_call_observations(correlation_id);\n"
        )
        assert text.count(correlation_index) == 1
        text = text.replace(correlation_index, "")
    target.write_text(text, encoding="utf-8")
    return target


def _schema_fingerprint(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    """Return ``(type, name, whitespace-normalised sql)`` for every master row."""
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    fingerprint: set[tuple[str, str, str]] = set()
    for row in rows:
        sql = " ".join((row["sql"] or "").split())
        fingerprint.add((row["type"], row["name"], sql))
    return fingerprint


def test_fresh_and_migrated_schemas_converge(tmp_path: Path) -> None:
    """Fresh schema.sql and a DB migrated through 0001→0003 must match."""
    fresh_path = tmp_path / "fresh.sqlite"
    Database(fresh_path).init_schema()
    fresh_conn = Database(fresh_path).get_connection()
    try:
        fresh_fp = _schema_fingerprint(fresh_conn)
        assert _user_version(Database(fresh_path)) == 3
        assert any(name == "section_index" for _type, name, _sql in fresh_fp)
    finally:
        fresh_conn.close()

    # Build a schema-1 database by applying the oldest schema, then migrate.
    schema_v1 = _historical_schema(tmp_path / "schema_v1.sql", schema_version=1)

    migrated_path = tmp_path / "migrated.sqlite"
    conn = sqlite3.connect(str(migrated_path))
    try:
        conn.executescript(schema_v1.read_text(encoding="utf-8"))
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()

    Database(migrated_path).init_schema()
    migrated_conn = Database(migrated_path).get_connection()
    try:
        migrated_fp = _schema_fingerprint(migrated_conn)
        assert _user_version(Database(migrated_path)) == 3
        # Migration 0002 invalidates stats on existing document rows.
        assert (
            migrated_conn.execute(
                "SELECT COUNT(*) FROM documents WHERE mtime_ns = -1 OR size_bytes = -1"
            ).fetchone()[0]
            == 0
        )  # empty documents table in this fixture
    finally:
        migrated_conn.close()

    assert fresh_fp == migrated_fp


def test_section_index_migration_invalidates_document_stats(tmp_path: Path) -> None:
    """Migration 0002 must force reconcile to re-read every existing document."""
    schema_v1 = _historical_schema(tmp_path / "schema_v1.sql", schema_version=1)

    db_path = tmp_path / "ferumind.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(schema_v1.read_text(encoding="utf-8"))
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            """INSERT INTO documents
               (project_key, path, id, title, folder, status, edit_policy,
                frontmatter_json, sha256, mtime_ns, size_bytes,
                created_at, updated_at, indexed_at)
               VALUES ('demo', 'spine.md', 'doc_spine', 'Spine', 'spine', 'active',
                       'propose-first', '{}', 'abc', 123, 456, 't', 't', 't')"""
        )
        conn.commit()
    finally:
        conn.close()

    Database(db_path).init_schema()
    conn = Database(db_path).get_connection()
    try:
        row = conn.execute(
            "SELECT mtime_ns, size_bytes, sha256 FROM documents WHERE path = 'spine.md'"
        ).fetchone()
        assert row is not None
        assert int(row["mtime_ns"]) == -1
        assert int(row["size_bytes"]) == -1
        assert row["sha256"] == "abc"
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='section_index'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def test_description_migration_preserves_rows_and_invalidates_document_stats(
    tmp_path: Path,
) -> None:
    """Migration 0003 adds descriptions and forces every document to reindex."""
    schema_v2 = _historical_schema(tmp_path / "schema_v2.sql", schema_version=2)
    db_path = tmp_path / "ferumind.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(schema_v2.read_text(encoding="utf-8"))
        conn.execute("PRAGMA user_version = 2")
        conn.execute(
            """INSERT INTO documents
               (project_key, path, id, title, folder, status, edit_policy,
                frontmatter_json, sha256, mtime_ns, size_bytes,
                created_at, updated_at, indexed_at)
               VALUES ('demo', 'spine.md', 'doc_spine', 'Spine', 'spine', 'active',
                       'propose-first', '{"existing":true}', 'abc', 123, 456,
                       'created', 'updated', 'indexed')"""
        )
        conn.execute(
            "INSERT INTO snapshots (id, project_key, snapshot_dir, reason, created_at)"
            " VALUES ('s', 'demo', '/tmp/s', 'apply_patch', 'created')"
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(db_path)
    db.init_schema()
    assert _user_version(db) == 3

    conn = db.get_connection()
    try:
        column = next(
            row
            for row in conn.execute("PRAGMA table_info(documents)")
            if row["name"] == "description"
        )
        row = conn.execute(
            "SELECT * FROM documents WHERE project_key = 'demo' AND path = 'spine.md'"
        ).fetchone()
        assert row is not None
        assert column["type"] == "TEXT"
        assert int(column["notnull"]) == 1
        assert column["dflt_value"] == "''"
        assert row["description"] == ""
        assert int(row["mtime_ns"]) == -1
        assert int(row["size_bytes"]) == -1
        assert row["sha256"] == "abc"
        assert row["frontmatter_json"] == '{"existing":true}'
        assert conn.execute("SELECT COUNT(*) FROM snapshots WHERE id = 's'").fetchone()[0] == 1
    finally:
        conn.close()
