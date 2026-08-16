"""Operator-invoked retention: reclaim Ferumind's own derived state.

A workspace accumulates state that is Ferumind's rather than the user's:
snapshot directories, migration backup tarballs, patch diffs kept in the
operation log, observation rows, blobs, and the private runtime log. None of
it was ever removed. This module removes it on demand, and only on demand.

**Nothing here runs by itself.** There is no scheduler, no startup hook, and
no MCP tool — an agent must not be able to reclaim the user's history. The
single entry point is :func:`prune_workspace`, called by ``ferumind prune``.

**User knowledge is out of bounds.** ``archive/`` in particular is *not*
garbage: it is where ``archive_document`` puts a document the user chose to
retire. Neither it nor ``memory/``, ``canvases/``, ``inbox/``, ``rules/``,
``compacts/``, ``library/``, or any ``spine.md`` is read, moved, or removed
here. Every path this module touches is resolved under ``.ferumind/``.

**The derived search index is out of bounds too.** ``documents`` and
``section_index`` are rebuildable, but ``rebuild_index`` and ``verify-index
--fix`` own them.

**Deleting rows frees no bytes on its own.** SQLite marks freed pages for
reuse and the file stays its old size, so a run that skips ``VACUUM`` looks
broken. The sequence is delete → ``wal_checkpoint(TRUNCATE)`` → ``VACUUM``,
and because ``VACUUM`` rewrites the database into a new file, a run refuses to
start without the free space to hold one.

Two rules make a partial run safe. Every step is independently repeatable — a
second run over a converged workspace deletes nothing — and a snapshot
directory is only ever removed when its name parses as the timestamped id
Ferumind wrote, so anything unrecognized is left exactly where it is.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import stat
from collections import Counter
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, Final

from pydantic import Field

from ferumind.core.blob_store import blob_store_root, stored_blobs, sweep_unreferenced
from ferumind.core.errors import FerumindError, ValidationError
from ferumind.core.locks import acquire_project_lock, acquire_workspace_lock
from ferumind.core.operations import (
    OP_APPLIED,
    OP_DISCARDED,
    OP_EXPIRED,
    OP_STALE,
    PROPOSAL_OP_TYPES,
    SOURCE_CLI,
    WORKSPACE_OPERATION_PROJECT,
    record_operation,
)
from ferumind.core.paths import (
    PathSafetyError,
    WorkspaceRoot,
    contained_path,
    contained_project_root,
    is_under_root,
)
from ferumind.core.registry import list_entries, require_project
from ferumind.core.runtime_events import RUNTIME_LOG_RELATIVE_PATH
from ferumind.core.types import DbConnection, JsonObject, JsonValue, StrictModel

#: Operation type recorded for the prune itself — counts only, never content.
PRUNE_OPERATION_TYPE: Final = "prune"

STORE_SNAPSHOTS: Final = "snapshots"
STORE_GLOBAL_SNAPSHOTS: Final = "global_snapshots"
STORE_DANGLING_SNAPSHOT_ROWS: Final = "dangling_snapshot_rows"
STORE_BLOBS: Final = "blobs"
STORE_MIGRATION_BACKUPS: Final = "migration_backups"
STORE_OPERATION_DIFFS: Final = "operation_diffs"
STORE_SPENT_PROPOSALS: Final = "spent_proposals"
STORE_OBSERVATIONS: Final = "observations"
STORE_RUNTIME_LOG: Final = "runtime_log"

_SNAPSHOTS_RELATIVE: Final = ".ferumind/snapshots"
_GLOBAL_SNAPSHOTS_RELATIVE: Final = ".ferumind/global-snapshots"
_BACKUPS_RELATIVE: Final = ".ferumind/backups"

#: ``<UTC timestamp>-<uuid4>``, exactly as ``core.snapshots`` writes it. A
#: directory that does not match is never removed: the name is the only
#: trustworthy record of when a snapshot was taken, and a directory Ferumind
#: did not name is not Ferumind's to delete.
_SNAPSHOT_DIR_NAME: Final = re.compile(
    r"\A(?P<stamp>\d{8}T\d{6})-(?P<snapshot_id>[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}"
    r"-[0-9a-fA-F]{12})\Z"
)
_SNAPSHOT_STAMP_FORMAT: Final = "%Y%m%dT%H%M%S"

#: ``workspace-backup-<%Y%m%dT%H%M%S%f>.tar.gz`` from ``migrate``.
_BACKUP_NAME: Final = re.compile(r"\Aworkspace-backup-\d{8}T\d{12}\.tar\.gz\Z")

#: Proposal states that hold no audit value. Their payloads were already
#: cleared when they left ``pending``, so the row is a husk. ``failed`` is
#: deliberately absent: an attempted mutation that did not complete is
#: history worth keeping.
_SPENT_PROPOSAL_STATES: Final[tuple[str, ...]] = (OP_EXPIRED, OP_DISCARDED, OP_STALE)

#: Flag-facing names for the policy bounds, in the order an operator meets
#: them. The CLI passes ``name=value`` strings straight through, so it never
#: has to know a field name and a typo is answered with this list.
POLICY_OVERRIDES: Final[Mapping[str, str]] = {
    "snapshot-days": "snapshot_max_age_days",
    "recent-snapshots": "keep_recent_snapshots",
    "diff-days": "diff_scrub_age_days",
    "proposal-days": "spent_proposal_max_age_days",
    "observation-days": "observation_max_age_days",
    "migration-backups": "keep_migration_backups",
    "runtime-log-bytes": "runtime_log_max_bytes",
}

#: Headroom demanded before ``VACUUM``: the database is rewritten into a new
#: file beside the old one, so the peak requirement is roughly twice its size.
#: The floor covers the rollback journal on a small database.
_VACUUM_HEADROOM_FLOOR_BYTES: Final = 16 * 1024 * 1024


class RetentionPrerequisiteError(FerumindError):
    """Raised when the workspace cannot safely be pruned right now.

    Nothing has been deleted when this is raised: the checks it reports run
    before the first removal. CLI-surface only — ``ferumind prune`` is the
    sole entry point and there is no MCP tool, so this code never reaches the
    tool surface (compare ``MigrationPrerequisiteError``).
    """

    code: ClassVar[str] = "RETENTION_PREREQUISITE_UNMET"


class RetentionPolicy(StrictModel):
    """How long Ferumind's own derived state is kept.

    These are local single-user defaults for one operator's own workspace,
    chosen to be dull rather than clever. They are not a product retention
    policy: hosted retention is NET-021's to decide and nothing here settles
    it.
    """

    #: Snapshot directories older than this are reclaimed. Snapshots are the
    #: only copies of superseded document versions, which is why the window is
    #: long and why `keep_recent_snapshots` backs it up.
    snapshot_max_age_days: int = Field(default=180, ge=1)
    #: Newest-first floor per project, applied before the age window, so a
    #: quiet project is never left with no recovery history at all.
    keep_recent_snapshots: int = Field(default=10, ge=0)
    #: Applied operations older than this keep every column but ``diff_text``.
    #: Thirty rather than ninety: the diffs are the one store that reliably
    #: holds reclaimable payload, and a ninety-day window on a workspace that
    #: had existed for thirty-five days could never touch any of it.
    diff_scrub_age_days: int = Field(default=30, ge=1)
    #: Spent proposal rows (expired/discarded/stale) older than this go.
    spent_proposal_max_age_days: int = Field(default=7, ge=1)
    #: Observation rows older than this go — the log is documented as
    #: "recent MCP calls", which nothing else currently makes true. Thirty
    #: days is what "recent" is worth for metadata nobody reads twice.
    observation_max_age_days: int = Field(default=30, ge=1)
    #: Migration backup tarballs to keep, newest first.
    keep_migration_backups: int = Field(default=2, ge=1)
    #: The private runtime log is rotated once it exceeds this size.
    runtime_log_max_bytes: int = Field(default=8 * 1024 * 1024, ge=64 * 1024)

    def validated(self) -> RetentionPolicy:
        """Return the policy, refusing values outside their declared bounds."""
        try:
            return RetentionPolicy.model_validate(self.model_dump())
        except ValueError as exc:
            raise ValidationError(f"Invalid retention policy: {exc}") from exc

    @classmethod
    def from_overrides(cls, overrides: Sequence[str]) -> RetentionPolicy:
        """Build a policy from ``name=value`` strings, defaults for the rest.

        Parsing lives here rather than in the CLI so the accepted names, their
        spelling, and the message for a typo have one home — and so a future
        caller that is not Typer gets the same answers.
        """
        values: dict[str, int] = {}
        for override in overrides:
            name, separator, raw = override.partition("=")
            field_name = POLICY_OVERRIDES.get(name.strip())
            if not separator or field_name is None:
                raise ValidationError(
                    f"Cannot read retention override {override!r}: expected name=value, "
                    f"where name is one of {', '.join(sorted(POLICY_OVERRIDES))}",
                    details={"accepted": list[JsonValue](sorted(POLICY_OVERRIDES))},
                )
            try:
                values[field_name] = int(raw)
            except ValueError as exc:
                raise ValidationError(
                    f"Retention override {name.strip()!r} needs a whole number, got {raw!r}"
                ) from exc
        return cls.model_validate(values).validated()


class StoreReclaim(StrictModel):
    """What one pass over one store found, and what it returned."""

    store: str
    #: Project key, or ``None`` for workspace-level state.
    scope: str | None = None
    #: Everything the store holds, whatever its age — never the post-cutoff
    #: subset. A dry run's whole job is telling the operator what is there and
    #: how much of it the current policy would take; counting only the expired
    #: rows made the database stores report ``0 of 0`` over 2,411 retained
    #: diffs and 7,236 observations, which reads as "nothing here" when the
    #: truth is "a lot here, none of it old enough yet".
    examined: int = 0
    reclaimed: int = 0
    #: Bytes returned to the filesystem. Zero for database stores — those
    #: report `database_bytes_freed` instead, and the file only shrinks at
    #: ``VACUUM``.
    bytes_reclaimed: int = 0
    #: Bytes freed inside the database, realized by ``VACUUM``.
    database_bytes_freed: int = 0
    #: Age in days of the oldest item still held, or ``None`` when the store
    #: is empty or its items carry no usable date.
    oldest_age_days: int | None = None
    #: The retention window this store is judged against, in days.
    window_days: int | None = None


class PruneReport(StrictModel):
    """The outcome of one prune, dry or real."""

    dry_run: bool
    policy: RetentionPolicy
    stores: list[StoreReclaim]
    projects: list[str]
    database_bytes_before: int
    database_bytes_after: int
    vacuumed: bool
    operation_id: str | None = None

    @property
    def bytes_reclaimed(self) -> int:
        """Bytes returned to the filesystem, excluding the database file."""
        return sum(entry.bytes_reclaimed for entry in self.stores)

    @property
    def database_bytes_freed(self) -> int:
        return sum(entry.database_bytes_freed for entry in self.stores)

    @property
    def database_bytes_reclaimed(self) -> int:
        return self.database_bytes_before - self.database_bytes_after

    @property
    def total_reclaimed(self) -> int:
        return sum(entry.reclaimed for entry in self.stores)

    def by_store(self) -> list[StoreReclaim]:
        """Collapse the per-project entries into one row per store.

        Thirteen projects produce twenty-six snapshot and blob rows, which is
        the detail ``--verbose`` exists for. The summary an operator reads
        first should be one line per kind of thing — and it must include the
        stores holding data that no window has caught yet, because "0 of 2411"
        is the number that tells them whether a shorter window is worth
        passing. Order follows first appearance, so the reading order is the
        order prune works in.
        """
        totals: dict[str, StoreReclaim] = {}
        for entry in self.stores:
            running = totals.get(entry.store)
            totals[entry.store] = StoreReclaim(
                store=entry.store,
                examined=entry.examined + (running.examined if running else 0),
                reclaimed=entry.reclaimed + (running.reclaimed if running else 0),
                bytes_reclaimed=entry.bytes_reclaimed + (running.bytes_reclaimed if running else 0),
                database_bytes_freed=entry.database_bytes_freed
                + (running.database_bytes_freed if running else 0),
                # The oldest thing across every project, and the window they
                # all share — a per-project split would say nothing here.
                oldest_age_days=max(
                    (
                        age
                        for age in (
                            entry.oldest_age_days,
                            running.oldest_age_days if running else None,
                        )
                        if age is not None
                    ),
                    default=None,
                ),
                window_days=entry.window_days or (running.window_days if running else None),
            )
        return list(totals.values())


@dataclass(frozen=True)
class _Context:
    """Everything a store pass needs, so no pass grows an argument list."""

    conn: DbConnection
    workspace: WorkspaceRoot
    policy: RetentionPolicy
    now: datetime
    dry_run: bool

    def cutoff(self, days: int) -> datetime:
        return self.now - timedelta(days=days)

    def cutoff_iso(self, days: int) -> str:
        return self.cutoff(days).isoformat()


@dataclass(frozen=True)
class _DatedSnapshot:
    """One snapshot directory, dated by the name Ferumind gave it."""

    taken_at: datetime
    snapshot_id: str
    directory: Path


@dataclass(frozen=True)
class _PayloadCensus:
    """What a set of doomed directories holds, by how it is referenced.

    ``direct_bytes`` are bytes only these directories name, freed the moment
    they are removed. ``inode_links`` counts names per shared inode, which is
    what lets a dry run say how much the blob sweep would then free without
    performing the removal to find out.
    """

    direct_bytes: int
    inode_links: Mapping[int, int]


# ── Entry point ──────────────────────────────────────────────────────────────


def prune_workspace(
    conn: DbConnection,
    workspace: WorkspaceRoot,
    *,
    policy: RetentionPolicy | None = None,
    project: str | None = None,
    dry_run: bool = True,
) -> PruneReport:
    """Reclaim Ferumind's derived state under *workspace*.

    ``dry_run`` is the default and writes nothing: the report shows exactly
    what a real run would remove. A real run holds the workspace lock and
    every visited project's lock for its whole duration, so it cannot
    interleave with a Ferumind write — but a live MCP server should be stopped
    first regardless, because ``VACUUM`` needs the database to itself and will
    fail loudly rather than racing for it.

    ``project`` narrows the run to one project's own state: its snapshots,
    blobs, operation diffs, and spent proposals. Workspace-level stores
    (migration backups, global snapshots, observations, the runtime log) are
    left alone in that case, since they belong to no single project.
    """
    active = (policy or RetentionPolicy()).validated()
    database = _database_file(conn)
    if not dry_run:
        _require_vacuum_headroom(database)

    roots = _prunable_projects(workspace, project)
    context = _Context(
        conn=conn,
        workspace=workspace,
        policy=active,
        now=datetime.now(UTC),
        dry_run=dry_run,
    )
    before = _file_bytes(database)
    stores: list[StoreReclaim] = []

    with ExitStack() as locks:
        locks.enter_context(acquire_workspace_lock(workspace))
        for key, root in roots.items():
            locks.enter_context(acquire_project_lock(root, key))

        for key, root in roots.items():
            stores.extend(_prune_project_files(context, key, root))
        stores.append(_scrub_applied_diffs(context, project))
        stores.append(_delete_spent_proposals(context, project))
        if project is None:
            stores.extend(_prune_workspace_files(context))
        stores.append(_clear_dangling_snapshot_rows(context, project))
        operation_id = _record_prune(context, stores)
        after = _compact_database(context, database, before)

    return PruneReport(
        dry_run=dry_run,
        policy=active,
        stores=stores,
        projects=list(roots),
        database_bytes_before=before,
        database_bytes_after=after,
        vacuumed=not dry_run and database is not None,
        operation_id=operation_id,
    )


def _prunable_projects(workspace: WorkspaceRoot, project: str | None) -> dict[str, Path]:
    """Return the projects to visit, keyed by project key.

    A registered project whose folder is gone is skipped rather than treated
    as an error: it has nothing on disk to reclaim, and cleaning up stale
    registry state is ``ferumind project delete``'s job, not prune's.
    """
    keys = (
        [require_project(workspace, project).key]
        if project is not None
        else [entry.key for entry in list_entries(workspace)]
    )
    roots: dict[str, Path] = {}
    for key in keys:
        try:
            root = contained_project_root(workspace, key)
        except PathSafetyError:
            continue
        if root.is_dir():
            roots[key] = root
    return roots


# ── Snapshot directories ─────────────────────────────────────────────────────


def _prune_project_files(context: _Context, key: str, root: Path) -> list[StoreReclaim]:
    """Reclaim one project's snapshot directories, then its unheld blobs."""
    snapshots, census = _prune_snapshot_directories(
        context,
        _existing_directory(root, _SNAPSHOTS_RELATIVE),
        STORE_SNAPSHOTS,
        key,
    )
    return [snapshots, _sweep_blobs(context, root, key, census)]


def _prune_snapshot_directories(
    context: _Context,
    base: Path | None,
    store: str,
    scope: str | None,
) -> tuple[StoreReclaim, _PayloadCensus]:
    """Reclaim the expired snapshot directories under *base*.

    The census travels back with the result because the blob sweep that
    follows needs it: on a dry run it is the only way to say what removing
    these directories would let the sweep free.
    """
    dated = _dated_snapshots(base)
    doomed = _expired_snapshots(context, dated)
    census = _census(doomed)
    reclaim = StoreReclaim(
        store=store,
        scope=scope,
        examined=len(dated),
        reclaimed=len(doomed),
        bytes_reclaimed=census.direct_bytes,
        oldest_age_days=_age_days(context, dated[-1].taken_at if dated else None),
        window_days=context.policy.snapshot_max_age_days,
    )
    if not context.dry_run:
        _remove_snapshots(context, doomed)
    return reclaim, census


def _expired_snapshots(context: _Context, dated: list[_DatedSnapshot]) -> list[_DatedSnapshot]:
    """Select the snapshots past the age window, newest-first floor applied.

    The floor comes off the top before the window is consulted, so a project
    whose every snapshot predates the window still keeps its most recent ones.
    """
    cutoff = context.cutoff(context.policy.snapshot_max_age_days)
    keep = context.policy.keep_recent_snapshots
    return [entry for entry in dated[keep:] if entry.taken_at < cutoff]


def _dated_snapshots(base: Path | None) -> list[_DatedSnapshot]:
    """Return every recognizable snapshot directory under *base*, newest first."""
    if base is None:
        return []
    found: list[_DatedSnapshot] = []
    for entry in sorted(base.iterdir()):
        match = _SNAPSHOT_DIR_NAME.match(entry.name)
        if match is None:
            continue
        try:
            # Re-resolved rather than used as walked: a symlink at any
            # component is refused here instead of followed into a delete.
            directory = contained_path(base, entry.name)
        except PathSafetyError:
            continue
        if not directory.is_dir():
            continue
        try:
            stamp = datetime.strptime(match["stamp"], _SNAPSHOT_STAMP_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            continue
        found.append(
            _DatedSnapshot(taken_at=stamp, snapshot_id=match["snapshot_id"], directory=directory)
        )
    found.sort(key=lambda entry: (entry.taken_at, entry.directory.name), reverse=True)
    return found


def _remove_snapshots(context: _Context, doomed: Sequence[_DatedSnapshot]) -> None:
    """Remove each directory and then its registry row, one at a time.

    Directory first: a row without its directory already answers
    ``SNAPSHOT_NOT_FOUND`` through the read path, whereas a directory without
    its row would be invisible to every listing while still costing its bytes.
    An interrupted run therefore leaves rows ``verify-index`` reports as
    ``dangling_snapshot`` and the next run clears; it never leaves storage
    nothing knows about.

    ``operations.snapshot_id`` is deliberately left in place. That column is
    the audit record's statement that the edit *was* snapshot-protected, and
    clearing it would make a protected edit read as an unprotected one. The
    id resolves to ``SNAPSHOT_NOT_FOUND``, which is the honest answer.
    """
    for entry in doomed:
        _remove_directory(entry.directory)
        context.conn.execute(
            "DELETE FROM snapshots WHERE id = ? OR snapshot_dir = ?",
            (entry.snapshot_id, str(entry.directory)),
        )
        context.conn.commit()


def _clear_dangling_snapshot_rows(context: _Context, project: str | None) -> StoreReclaim:
    """Delete registry rows whose snapshot directory is already gone.

    Removal takes the directory first and the row second, so a run killed
    between the two leaves a row pointing at nothing — which is precisely what
    ``verify-index`` reports as ``dangling_snapshot``. Clearing them here is
    what makes an interrupted prune something the next prune finishes, rather
    than a finding that stays on the books forever. It also picks up rows left
    by a directory the operator removed by hand.

    A row whose path escapes the workspace is left alone: this module deletes
    nothing on the strength of a path it cannot vouch for, and ``verify-index``
    already reports that case.
    """
    sql = "SELECT id, snapshot_dir FROM snapshots"
    params: list[object] = []
    if project is not None:
        sql += " WHERE project_key = ?"
        params.append(project)
    doomed: list[str] = []
    for row in context.conn.execute(sql, tuple(params)).fetchall():
        directory = Path(str(row["snapshot_dir"]))
        if not is_under_root(directory, Path(context.workspace)) or directory.exists():
            continue
        doomed.append(str(row["id"]))
    if not context.dry_run and doomed:
        context.conn.executemany(
            "DELETE FROM snapshots WHERE id = ?", [(entry,) for entry in doomed]
        )
        context.conn.commit()
    return StoreReclaim(
        store=STORE_DANGLING_SNAPSHOT_ROWS,
        scope=project,
        examined=len(doomed),
        reclaimed=len(doomed),
    )


def _remove_directory(directory: Path) -> None:
    """Remove a snapshot directory without ever following a symlink."""
    if directory.is_symlink():
        return
    shutil.rmtree(directory, ignore_errors=False)


# ── Blobs ────────────────────────────────────────────────────────────────────


def _sweep_blobs(
    context: _Context,
    base: Path,
    scope: str | None,
    census: _PayloadCensus,
) -> StoreReclaim:
    """Unlink blobs nothing else holds, or project what a sweep would free.

    A real run sweeps after the snapshot directories are gone, so ``st_nlink``
    already answers the question. A dry run cannot look at that, so it
    subtracts the links the doomed directories were about to release and asks
    which blobs would be left holding only their own name.
    """
    store_root = blob_store_root(base)
    if not context.dry_run:
        result = sweep_unreferenced(store_root)
        return StoreReclaim(
            store=STORE_BLOBS,
            scope=scope,
            examined=result.removed + result.kept,
            reclaimed=result.removed,
            bytes_reclaimed=result.bytes_reclaimed,
        )

    examined = removed = reclaimed_bytes = 0
    for blob in stored_blobs(store_root):
        try:
            status = os.stat(blob, follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(status.st_mode):
            continue
        examined += 1
        if status.st_nlink - census.inode_links.get(status.st_ino, 0) <= 1:
            removed += 1
            reclaimed_bytes += status.st_size
    return StoreReclaim(
        store=STORE_BLOBS,
        scope=scope,
        examined=examined,
        reclaimed=removed,
        bytes_reclaimed=reclaimed_bytes,
    )


def _census(doomed: Sequence[_DatedSnapshot]) -> _PayloadCensus:
    """Measure what removing *doomed* frees now, and what it would release.

    A payload that shares its inode with a blob (and often with the live file
    too) frees nothing when its snapshot goes; only the sweep that follows can
    free it. Counting those separately is what keeps a reported byte a byte
    that actually came back.
    """
    direct = 0
    links: Counter[int] = Counter()
    for entry in doomed:
        for status in _regular_files(entry.directory):
            if status.st_nlink == 1:
                direct += status.st_size
            else:
                links[status.st_ino] += 1
    return _PayloadCensus(direct_bytes=direct, inode_links=links)


def _regular_files(directory: Path) -> Iterator[os.stat_result]:
    """Yield a stat for every regular file below *directory*, never following links."""
    for parent, _, names in os.walk(directory, followlinks=False):
        for name in names:
            try:
                status = os.lstat(Path(parent) / name)
            except OSError:
                continue
            if stat.S_ISREG(status.st_mode):
                yield status


# ── Migration backups ────────────────────────────────────────────────────────


def _prune_migration_backups(context: _Context) -> StoreReclaim:
    """Keep the newest N migration tarballs and remove the rest.

    Count-bounded rather than age-bounded on purpose: the value of a backup is
    that it is the state before the last migration, and that does not decay
    with the calendar. The names are fixed-width timestamps, so sorting them
    as strings sorts them chronologically.
    """
    base = _existing_directory(Path(context.workspace), _BACKUPS_RELATIVE)
    if base is None:
        return StoreReclaim(store=STORE_MIGRATION_BACKUPS)
    tarballs = sorted(
        (entry for entry in base.iterdir() if _BACKUP_NAME.match(entry.name)),
        key=lambda entry: entry.name,
        reverse=True,
    )
    doomed = tarballs[context.policy.keep_migration_backups :]
    reclaimed = reclaimed_bytes = 0
    for entry in doomed:
        try:
            status = entry.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(status.st_mode):
            continue
        reclaimed += 1
        if status.st_nlink == 1:
            reclaimed_bytes += status.st_size
        if not context.dry_run:
            with suppress(OSError):
                entry.unlink()
    return StoreReclaim(
        store=STORE_MIGRATION_BACKUPS,
        examined=len(tarballs),
        reclaimed=reclaimed,
        bytes_reclaimed=reclaimed_bytes,
    )


# ── Operation log ────────────────────────────────────────────────────────────


def _scrub_applied_diffs(context: _Context, project: str | None) -> StoreReclaim:
    """Clear ``diff_text`` on old applied rows and keep every other column.

    The operation row *is* the auditable history README promises — who, what,
    when, which path, which hashes, which snapshot. The diff is its payload
    and the bulk of its size. ``mark_operation_state`` already clears the
    payload when a proposal leaves ``pending``; applied rows escape it because
    ``apply_patch`` inserts a separate row already in ``applied`` state, which
    was never pending and so never met that clause. This is that leak, closed
    after the fact rather than by deleting the record.
    """
    held, params = _scoped("state = ? AND diff_text IS NOT NULL", [OP_APPLIED], project)
    expired = f"{held} AND created_at < ?"
    expired_params = [*params, context.cutoff_iso(context.policy.diff_scrub_age_days)]

    # S608 here and below: every clause is assembled from module constants and
    # the fixed ``_scoped`` suffix; all values stay bound parameters.
    present = _count(context, f"SELECT COUNT(*) FROM operations WHERE {held}", params)  # noqa: S608
    row = context.conn.execute(
        f"SELECT COUNT(*) AS n, "  # noqa: S608
        f"COALESCE(SUM(length(CAST(diff_text AS BLOB))), 0) AS bytes "
        f"FROM operations WHERE {expired}",
        tuple(expired_params),
    ).fetchone()
    scrubbed, freed = int(row["n"]), int(row["bytes"])
    if not context.dry_run and scrubbed:
        context.conn.execute(
            f"UPDATE operations SET diff_text = NULL WHERE {expired}",  # noqa: S608
            tuple(expired_params),
        )
        context.conn.commit()
    return StoreReclaim(
        store=STORE_OPERATION_DIFFS,
        scope=project,
        examined=present,
        reclaimed=scrubbed,
        database_bytes_freed=freed,
        oldest_age_days=_oldest_row_age(
            context,
            f"SELECT MIN(created_at) FROM operations WHERE {held}",  # noqa: S608
            params,
        ),
        window_days=context.policy.diff_scrub_age_days,
    )


def _delete_spent_proposals(context: _Context, project: str | None) -> StoreReclaim:
    """Delete expired, discarded, and stale proposal rows outright.

    Their request payload and diff were cleared the moment they left
    ``pending``, so what remains is a husk recording that somebody proposed an
    edit and did not make it. ``failed`` rows are kept: an attempted mutation
    that did not complete is worth being able to look up.
    """
    states = ",".join("?" for _ in _SPENT_PROPOSAL_STATES)
    types = ",".join("?" for _ in PROPOSAL_OP_TYPES)
    held, params = _scoped(
        f"state IN ({states}) AND operation_type IN ({types})",
        [*_SPENT_PROPOSAL_STATES, *sorted(PROPOSAL_OP_TYPES)],
        project,
    )
    expired = f"{held} AND created_at < ?"
    expired_params = [*params, context.cutoff_iso(context.policy.spent_proposal_max_age_days)]

    # S608: both placeholder lists are built from closed module constants;
    # every value below remains a bound parameter.
    present = _count(context, f"SELECT COUNT(*) FROM operations WHERE {held}", params)  # noqa: S608
    doomed = _count(context, f"SELECT COUNT(*) FROM operations WHERE {expired}", expired_params)  # noqa: S608
    if not context.dry_run and doomed:
        context.conn.execute(
            f"DELETE FROM operations WHERE {expired}",  # noqa: S608
            tuple(expired_params),
        )
        context.conn.commit()
    return StoreReclaim(
        store=STORE_SPENT_PROPOSALS,
        scope=project,
        examined=present,
        reclaimed=doomed,
        oldest_age_days=_oldest_row_age(
            context,
            f"SELECT MIN(created_at) FROM operations WHERE {held}",  # noqa: S608
            params,
        ),
        window_days=context.policy.spent_proposal_max_age_days,
    )


def _age_days(context: _Context, moment: datetime | None) -> int | None:
    """Return how many whole days ago *moment* was, or ``None``."""
    if moment is None or moment.utcoffset() is None:
        return None
    return max(0, (context.now - moment).days)


def _oldest_row_age(context: _Context, sql: str, params: list[object]) -> int | None:
    """Return the age in days of the oldest ``created_at`` the query selects."""
    row = context.conn.execute(sql, tuple(params)).fetchone()
    stamp = None if row is None else row[0]
    if not isinstance(stamp, str):
        return None
    try:
        return _age_days(context, datetime.fromisoformat(stamp))
    except ValueError:
        return None


def _count(context: _Context, sql: str, params: list[object]) -> int:
    """Run a single-column COUNT and return it."""
    return int(context.conn.execute(sql, tuple(params)).fetchone()[0])


def _scoped(clause: str, params: list[object], project: str | None) -> tuple[str, list[object]]:
    """Append a project filter to *clause* when the run is scoped to one."""
    if project is None:
        return clause, params
    return f"{clause} AND project_key = ?", [*params, project]


# ── Observations ─────────────────────────────────────────────────────────────


def _prune_observations(context: _Context) -> StoreReclaim:
    """Age-bound the MCP call observation log.

    README describes it as "recent MCP calls"; nothing has made that true
    until now. The rows are metadata only, so there is no payload to preserve
    and the whole row goes.
    """
    cutoff = context.cutoff_iso(context.policy.observation_max_age_days)
    present = _count(context, "SELECT COUNT(*) FROM mcp_call_observations", [])
    doomed = _count(
        context, "SELECT COUNT(*) FROM mcp_call_observations WHERE created_at < ?", [cutoff]
    )
    if not context.dry_run and doomed:
        context.conn.execute(
            "DELETE FROM mcp_call_observations WHERE created_at < ?",
            (cutoff,),
        )
        context.conn.commit()
    return StoreReclaim(
        store=STORE_OBSERVATIONS,
        examined=present,
        reclaimed=doomed,
        oldest_age_days=_oldest_row_age(
            context, "SELECT MIN(created_at) FROM mcp_call_observations", []
        ),
        window_days=context.policy.observation_max_age_days,
    )


# ── Private runtime log ──────────────────────────────────────────────────────


def _rotate_runtime_log(context: _Context) -> StoreReclaim:
    """Rotate the append-only JSONL once it outgrows its bound.

    One generation is kept, so the log costs at most twice the bound instead
    of growing forever — and ``read_runtime_events``, which rescans the whole
    file on every read, stops paying for history nobody asked for. The bytes
    actually returned are the previous generation's, which this replaces.

    The rename happens under the same exclusive lock the writer takes, so a
    concurrent append lands wholly on one side of the rotation.
    """
    log = contained_path(Path(context.workspace), RUNTIME_LOG_RELATIVE_PATH)
    rotated = contained_path(Path(context.workspace), f"{RUNTIME_LOG_RELATIVE_PATH}.1")
    if not log.is_file() or log.is_symlink():
        return StoreReclaim(store=STORE_RUNTIME_LOG)
    if log.stat().st_size <= context.policy.runtime_log_max_bytes:
        return StoreReclaim(store=STORE_RUNTIME_LOG, examined=1)

    reclaimed_bytes = rotated.stat().st_size if rotated.is_file() else 0
    if not context.dry_run:
        with _locked(log):
            os.replace(log, rotated)
    return StoreReclaim(
        store=STORE_RUNTIME_LOG,
        examined=1,
        reclaimed=1,
        bytes_reclaimed=reclaimed_bytes,
    )


@contextmanager
def _locked(target: Path) -> Generator[None]:
    """Hold *target*'s advisory lock, matching the runtime log's own writer."""
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ── Workspace-level orchestration ────────────────────────────────────────────


def _prune_workspace_files(context: _Context) -> list[StoreReclaim]:
    """Run the stores that belong to no single project.

    Global snapshots capture workspace mutations — project creation, compact
    writes, the format marker at migration — and are registered in the
    ``snapshots`` table under a reserved key, so a row goes with its directory
    exactly as a project snapshot's does. The workspace blob store sweeps
    last, after the directories that were holding its payloads are gone.
    """
    global_snapshots, census = _prune_snapshot_directories(
        context,
        _existing_directory(Path(context.workspace), _GLOBAL_SNAPSHOTS_RELATIVE),
        STORE_GLOBAL_SNAPSHOTS,
        None,
    )
    return [
        global_snapshots,
        _prune_migration_backups(context),
        _prune_observations(context),
        _rotate_runtime_log(context),
        _sweep_blobs(context, Path(context.workspace), None, census),
    ]


def _record_prune(context: _Context, stores: Sequence[StoreReclaim]) -> str | None:
    """Log the prune itself: counts and byte totals, never content.

    Recorded before ``VACUUM`` so the row is in the file the vacuum rewrites,
    and skipped entirely on a dry run, which must leave no trace.
    """
    if context.dry_run:
        return None
    counts: JsonObject = {
        f"{entry.store}:{entry.scope}" if entry.scope else entry.store: entry.reclaimed
        for entry in stores
    }
    request: dict[str, JsonValue] = {
        "reclaimed": counts,
        "bytes_reclaimed": sum(entry.bytes_reclaimed for entry in stores),
        "database_bytes_freed": sum(entry.database_bytes_freed for entry in stores),
    }
    return record_operation(
        context.conn,
        project_key=WORKSPACE_OPERATION_PROJECT,
        operation_type=PRUNE_OPERATION_TYPE,
        tool_name=None,
        source=SOURCE_CLI,
        request_json=request,
        state=OP_APPLIED,
    )


# ── Database compaction ──────────────────────────────────────────────────────


def _compact_database(context: _Context, database: Path | None, before: int) -> int:
    """Checkpoint the WAL and rewrite the database, returning its new size.

    Without this the deletions above free exactly nothing: SQLite marks the
    pages free for reuse and the file keeps its old size. ``VACUUM`` cannot
    run inside a transaction, so the pending work is committed first.
    """
    if context.dry_run or database is None:
        return before
    context.conn.commit()
    context.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    context.conn.execute("VACUUM")
    return _file_bytes(database)


def _require_vacuum_headroom(database: Path | None) -> None:
    """Refuse to start when ``VACUUM`` could not finish.

    ``VACUUM`` writes a whole new copy of the database before replacing the
    original, so a disk with less free space than that is a disk where the
    run fills the filesystem instead of reclaiming it. Checked before the
    first deletion, so a refusal means nothing was touched.
    """
    if database is None:
        return
    required = _file_bytes(database) + _VACUUM_HEADROOM_FLOOR_BYTES
    try:
        free = shutil.disk_usage(database.parent).free
    except OSError as exc:
        raise RetentionPrerequisiteError(
            "Cannot determine free disk space for the database rewrite"
        ) from exc
    if free >= required:
        return
    raise RetentionPrerequisiteError(
        "Not enough free disk space to rewrite the database: "
        f"{required} bytes needed, {free} available. Free some space and run prune again.",
        details={"required_bytes": required, "free_bytes": free},
    )


def _database_file(conn: DbConnection) -> Path | None:
    """Return the main database's path, or ``None`` when it has no file."""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if str(row[1]) == "main":
            location = str(row[2])
            return Path(location) if location else None
    return None


def _file_bytes(target: Path | None) -> int:
    """Return the size of *target* plus its WAL, or zero when absent."""
    if target is None:
        return 0
    total = 0
    for candidate in (target, target.with_name(f"{target.name}-wal")):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def _existing_directory(base: Path, relative: str) -> Path | None:
    """Resolve *relative* under *base*, or ``None`` when it is not a directory."""
    try:
        resolved = contained_path(base, relative)
    except PathSafetyError:
        return None
    return resolved if resolved.is_dir() else None
