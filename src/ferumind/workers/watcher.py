"""Filesystem watcher worker: the liveness layer for out-of-band edits.

Mechanical only (00 D13): no LLM, no judgment. Debounces filesystem events
per file (coalesce window: 5 s of quiet, max one snapshot per file per
60 s) and takes a snapshot-on-detect plus reindex + operation-log entry via
:func:`ferumind.core.reconcile.record_watch_detection`. Watcher failure modes
(server down during the edit, synced mounts, rename-based saves, event
overflow) are covered by reconcile-on-read — the correctness floor.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from ferumind.core.locks import acquire_project_lock
from ferumind.core.paths import WorkspaceRoot, contained_path, contained_project_root
from ferumind.core.reconcile import reconcile_document, record_watch_detection
from ferumind.core.registry import require_project
from ferumind.db.database import Database

logger = logging.getLogger(__name__)

COALESCE_QUIET_SECONDS = 5.0
MIN_SNAPSHOT_INTERVAL_SECONDS = 60.0
MAX_SNAPSHOT_TRACKING_KEYS = 10_000
SNAPSHOT_TRACKING_TTL_SECONDS = 60 * 60
MAX_PENDING_EVENT_KEYS = 10_000


@dataclass
class FileDebouncer:
    """Pure per-file debounce bookkeeping for watch events.

    ``note_event`` records activity; ``take_due`` returns the files whose
    coalesce window has closed (quiet for *quiet_seconds*) and clears them.
    ``allow_snapshot`` rate-limits snapshots to one per file per
    *min_snapshot_interval*.
    """

    quiet_seconds: float = COALESCE_QUIET_SECONDS
    min_snapshot_interval: float = MIN_SNAPSHOT_INTERVAL_SECONDS
    _last_event: OrderedDict[str, float] = field(default_factory=lambda: OrderedDict[str, float]())
    _last_snapshot: OrderedDict[str, float] = field(
        default_factory=lambda: OrderedDict[str, float]()
    )

    def note_event(self, key: str, now: float) -> None:
        self._last_event[key] = now
        self._last_event.move_to_end(key)
        while len(self._last_event) > MAX_PENDING_EVENT_KEYS:
            self._last_event.popitem(last=False)

    def take_due(self, now: float) -> list[str]:
        due = [key for key, last in self._last_event.items() if now - last >= self.quiet_seconds]
        for key in due:
            del self._last_event[key]
        return sorted(due)

    def pending_count(self) -> int:
        return len(self._last_event)

    def snapshot_tracking_count(self) -> int:
        return len(self._last_snapshot)

    def allow_snapshot(self, key: str, now: float) -> bool:
        cutoff = now - max(SNAPSHOT_TRACKING_TTL_SECONDS, self.min_snapshot_interval * 2)
        while self._last_snapshot:
            oldest_key, oldest_at = next(iter(self._last_snapshot.items()))
            if oldest_at >= cutoff:
                break
            del self._last_snapshot[oldest_key]
        last = self._last_snapshot.get(key)
        if last is not None and now - last < self.min_snapshot_interval:
            return False
        self._last_snapshot[key] = now
        self._last_snapshot.move_to_end(key)
        while len(self._last_snapshot) > MAX_SNAPSHOT_TRACKING_KEYS:
            self._last_snapshot.popitem(last=False)
        return True

    def release_snapshot_reservation(self, key: str, reserved_at: float) -> None:
        """Allow a failed snapshot attempt to be retried on the next event."""
        if self._last_snapshot.get(key) == reserved_at:
            del self._last_snapshot[key]


def classify_event_path(workspace_root: Path, changed_path: Path) -> tuple[str, str] | None:
    """Map an absolute changed path to ``(project_key, project-relative path)``.

    Returns ``None`` for paths outside ``projects/``, non-Markdown files, and
    hidden/``.ferumind`` internals (snapshot writes must never feed back into
    the watcher).
    """
    try:
        rel = changed_path.resolve().relative_to(Path(workspace_root).resolve())
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 3 or parts[0] != "projects":
        return None
    project_key = parts[1]
    doc_parts = parts[2:]
    if any(part.startswith(".") for part in (project_key, *doc_parts)):
        return None
    if not doc_parts[-1].endswith(".md"):
        return None
    return project_key, "/".join(doc_parts)


def handle_detected_change(
    database: Database,
    workspace_root: WorkspaceRoot,
    project_key: str,
    rel_path: str,
    *,
    snapshot: bool,
) -> None:
    """Process one debounced change: snapshot (if allowed) + reconcile."""
    entry = require_project(workspace_root, project_key)
    project_dir = contained_project_root(workspace_root, entry.key)
    with acquire_project_lock(project_dir, entry.key):
        conn = database.get_connection()
        try:
            if snapshot:
                record_watch_detection(conn, workspace_root, entry.key, rel_path)
            else:
                reconcile_document(conn, workspace_root, entry.key, rel_path, source="watcher")
        finally:
            conn.close()


async def watch_workspace(  # pragma: no cover - event-loop wiring; logic lives in the helpers
    database: Database,
    workspace_root: WorkspaceRoot,
    *,
    debouncer: FileDebouncer | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the watch loop over ``workspace/projects`` until *stop_event* is set."""
    from collections.abc import AsyncGenerator, Callable
    from typing import cast

    import watchfiles

    # watchfiles' awatch signature is partially unknown to pyright (its
    # stop_event union references a private anyio type); pin the shape we use.
    awatch_typed = cast(
        "Callable[..., AsyncGenerator[set[tuple[watchfiles.Change, str]], None]]",
        watchfiles.awatch,  # pyright: ignore[reportUnknownMemberType] - see cast note above
    )

    bouncer = debouncer or FileDebouncer()
    projects_dir = contained_path(workspace_root, "projects")
    projects_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    projects_dir.chmod(0o700)

    async def flush_loop() -> None:
        while stop_event is None or not stop_event.is_set():
            await asyncio.sleep(1.0)
            now = time.monotonic()
            for key in bouncer.take_due(now):
                project_key, rel_path = key.split("\x00", 1)
                snapshot = bouncer.allow_snapshot(key, now)
                try:
                    handle_detected_change(
                        database, workspace_root, project_key, rel_path, snapshot=snapshot
                    )
                except (OSError, ValueError, sqlite3.Error) as exc:
                    if snapshot:
                        bouncer.release_snapshot_reservation(key, now)
                    # Requeue known transient failures. Reconcile-on-read is
                    # still the correctness floor, while this preserves the
                    # watcher's liveness when no subsequent filesystem event
                    # arrives.
                    bouncer.note_event(key, time.monotonic())
                    logger.error(
                        "Watcher failed to reconcile a change (type=%s)",
                        type(exc).__name__,
                    )

    flusher = asyncio.create_task(flush_loop())
    try:
        async for changes in awatch_typed(projects_dir, stop_event=stop_event):
            now = time.monotonic()
            for _change, raw_path in changes:
                classified = classify_event_path(workspace_root, Path(raw_path))
                if classified is None:
                    continue
                project_key, rel_path = classified
                bouncer.note_event(f"{project_key}\x00{rel_path}", now)
    finally:
        flusher.cancel()
