"""SQLite connection management and the numbered-migration framework.

Fresh databases receive the current ``schema.sql`` and jump straight to the
latest schema version — history is never replayed. Existing databases apply
the numbered migrations in ``db/migrations/`` (``0001_<slug>.sql``, applied
in order inside a transaction) whose number exceeds ``PRAGMA user_version``
(product/spec-versioning.md §2.4). DB migrations run automatically at
startup: the database is system state, unlike the workspace format which
migrates only on explicit ``ferumind migrate``.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

_MIGRATION_FILE_RE = re.compile(r"(\d{4})_[a-z0-9_]+\.sql")

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class MigrationError(RuntimeError):
    """Raised when the migration chain is malformed or fails to apply."""


@dataclass(frozen=True)
class Migration:
    number: int
    path: Path


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return the numbered migrations in *migrations_dir*, ordered and validated.

    Numbers must be unique and contiguous starting at 1 so a database can
    never skip a step silently.
    """
    if not migrations_dir.is_dir():
        return []
    migrations: list[Migration] = []
    for entry in sorted(migrations_dir.iterdir()):
        if not entry.is_file():
            continue
        match = _MIGRATION_FILE_RE.fullmatch(entry.name)
        if match is None:
            if entry.suffix == ".sql":
                msg = f"Migration file name {entry.name!r} does not match NNNN_slug.sql"
                raise MigrationError(msg)
            continue
        migrations.append(Migration(number=int(match.group(1)), path=entry))
    migrations.sort(key=lambda m: m.number)
    for index, migration in enumerate(migrations, start=1):
        if migration.number != index:
            msg = (
                f"Migration numbers must be contiguous from 1; "
                f"found {migration.path.name} at position {index}"
            )
            raise MigrationError(msg)
    return migrations


class Database:
    """SQLite database manager: per-call connections, WAL, migrations.

    Connections are opened per call (not long-lived) for stdio concurrency;
    every connection sets ``busy_timeout`` because it is a per-connection
    setting.
    """

    def __init__(self, db_path: Path, *, migrations_dir: Path = MIGRATIONS_DIR) -> None:
        self._db_path = db_path
        self._migrations_dir = migrations_dir

    @property
    def db_path(self) -> Path:
        return self._db_path

    def get_connection(self) -> sqlite3.Connection:
        """Open a new connection with WAL mode and a busy timeout."""
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA trusted_schema = OFF")
        conn.execute("PRAGMA synchronous = FULL")
        conn.row_factory = sqlite3.Row
        return conn

    def get_readonly_connection(self) -> sqlite3.Connection:
        """Open the existing database without creating or mutating it.

        SQLite's URI ``mode=ro`` is the filesystem-level guard: unlike
        ``PRAGMA query_only``, it also prevents writes that happen before a
        pragma can be installed and refuses to create a missing database.
        ``query_only`` remains as defense in depth.  This opener deliberately
        does not request WAL mode, initialize the schema, or run migrations.
        """

        encoded_path = quote(str(self._db_path.resolve(strict=False)), safe="/")
        conn = sqlite3.connect(f"file:{encoded_path}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA trusted_schema = OFF")
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def init_schema(self) -> None:
        """Create or migrate the schema to the latest version."""
        # The database is Ferumind's own state, so its directory mode is
        # re-asserted on every init rather than only set on create. See
        # ``core.file_io`` for why operator-owned directories differ.
        self._db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._db_path.parent.chmod(0o700)
        migrations = discover_migrations(self._migrations_dir)
        latest = migrations[-1].number if migrations else 0
        conn = self.get_connection()
        try:
            if _is_fresh(conn):
                _apply_baseline_schema(conn, latest=latest)
                return
            current = _user_version(conn)
            if current > latest:
                msg = (
                    f"Database schema version {current} is newer than this build "
                    f"supports ({latest}); refusing to run against it"
                )
                raise MigrationError(msg)
            for migration in migrations:
                if migration.number <= current:
                    continue
                _apply_migration(conn, migration)
        finally:
            conn.close()
            if self._db_path.exists():
                self._db_path.chmod(0o600)


def _is_fresh(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table'").fetchone()
    return int(row["n"]) == 0


def _user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _apply_migration(conn: sqlite3.Connection, migration: Migration) -> None:
    sql = migration.path.read_text(encoding="utf-8")
    # autocommit=False keeps executescript inside one transaction; the legacy
    # default would implicitly commit before running the script.
    previous_autocommit = conn.autocommit
    conn.autocommit = False
    try:
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {migration.number}")
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        msg = f"Migration {migration.path.name} failed: {exc}"
        raise MigrationError(msg) from exc
    finally:
        conn.autocommit = previous_autocommit


def _apply_baseline_schema(conn: sqlite3.Connection, *, latest: int) -> None:
    """Install a fresh schema transactionally so a failed init is retryable."""
    previous_autocommit = conn.autocommit
    conn.autocommit = False
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(f"PRAGMA user_version = {latest}")
        conn.commit()
    except (OSError, sqlite3.Error) as exc:
        conn.rollback()
        raise MigrationError("Fresh database schema initialization failed") from exc
    finally:
        conn.autocommit = previous_autocommit
