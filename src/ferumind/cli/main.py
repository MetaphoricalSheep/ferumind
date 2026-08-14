# pyright: reportUnusedFunction=false
"""Ferumind CLI.

Commands land as the phases in product/roadmap.md do; the current surface
covers serving the MCP server, workspace migration, and index maintenance.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import typer

from ferumind.cli.common import (
    WORKSPACE_OPTION as _WORKSPACE_OPTION,
)
from ferumind.cli.common import (
    database_path as _database_path,
)
from ferumind.cli.common import (
    workspace_root as _workspace_root,
)
from ferumind.cli.diagnostics import dashboard_command, doctor_command, observations_app

if TYPE_CHECKING:  # imported for typing only; runtime imports stay function-local
    from ferumind.core.images import ImagePolicy
    from ferumind.core.lint import LintReport
    from ferumind.core.paths import WorkspaceRoot

app = typer.Typer(
    help="Ferumind — local-first, Markdown-backed knowledge workspace.",
    no_args_is_help=True,
)
mcp_app = typer.Typer(help="MCP server commands.")
compact_app = typer.Typer(help="Workspace-level compact commands.")
project_app = typer.Typer(help="Project administration commands.")
app.add_typer(mcp_app, name="mcp")
app.add_typer(compact_app, name="compact")
app.add_typer(project_app, name="project")
app.add_typer(observations_app, name="observations")
app.command("doctor")(doctor_command)
app.command("dashboard")(dashboard_command)


@app.callback()
def _configure(ctx: typer.Context) -> None:
    """Apply FERUMIND_LOG_LEVEL before any command runs.

    Runs once per invocation, ahead of every subcommand. Logging goes to
    stderr so ``ferumind mcp serve`` never contaminates the JSON-RPC stream on
    stdout.
    """
    from ferumind.core.config import load_config
    from ferumind.core.logging_setup import configure_logging

    if ctx.resilient_parsing:  # shell completion — do not touch process state
        return
    configure_logging(load_config().log_level)


def _initialized_workspace_root(workspace: Path | None) -> Path:
    """Resolve ``--workspace`` and refuse anything that is not already a workspace.

    Maintenance commands read and rewrite an existing workspace; none of them is
    a creating path, and ``scripts/bootstrap_workspace.py`` is. Without this
    check a mistyped ``--workspace`` reaches ``Database.init_schema``, which
    creates the directory and an empty SQLite file on the way past — and then
    the command reports success, because a freshly created database is
    indistinguishable from a correctly reindexed empty workspace. The operator
    is told the run worked on a path that has never existed.

    An existing directory is not enough either: without a format marker it was
    never bootstrapped. :func:`read_format` returns ``None`` for "unknown" and
    says callers must not substitute a number for it, so this refuses rather
    than guessing.

    ``info`` deliberately does not use this — reporting an uninitialized
    workspace is part of its job.
    """
    from ferumind.core.format import read_format
    from ferumind.core.paths import WorkspaceRoot

    root = _workspace_root(workspace)
    if not root.is_dir():
        typer.echo(
            f"No workspace at {root}. Check the path, or run "
            "scripts/bootstrap_workspace.py to create one.",
            err=True,
        )
        raise typer.Exit(code=1)
    if read_format(WorkspaceRoot(root)) is None:
        typer.echo(
            f"{root} is not an initialized workspace: no readable format marker "
            "at system/meta.yml. Run scripts/bootstrap_workspace.py.",
            err=True,
        )
        raise typer.Exit(code=1)
    return root


def _project_keys(ws: WorkspaceRoot, project: str | None) -> list[str]:
    """Resolve ``--project`` to the keys a maintenance pass should visit.

    ``None`` means every registered project. A key that is not registered is a
    user mistake, not a bug, so it exits with the message rather than letting
    ``ProjectNotFoundError`` reach the interpreter and print a traceback.
    """
    from ferumind.core.errors import FerumindError
    from ferumind.core.registry import list_entries, require_project

    if project is None:
        return [entry.key for entry in list_entries(ws)]
    try:
        return [require_project(ws, project).key]
    except FerumindError as exc:
        typer.echo(f"Cannot resolve project: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _storage_policy(
    workspace: Path | None,
    *,
    max_edge: int | None,
    quality: int | None,
) -> ImagePolicy:
    """Build the image policy for one run, applying any command-line overrides.

    Validated once, up front: a bad override must fail before the first file is
    touched rather than partway through the workspace, and it must say what the
    accepted range is instead of raising through the interpreter.
    """
    from ferumind.core.config import load_config
    from ferumind.core.errors import FerumindError

    policy = load_config(workspace).image_policy
    if max_edge is not None:
        policy = policy.model_copy(update={"max_edge": max_edge})
    if quality is not None:
        policy = policy.model_copy(update={"jpeg_quality": quality})
    try:
        return policy.validated()
    except FerumindError as exc:
        typer.echo(f"Cannot compress images: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def info(
    workspace: Annotated[Path | None, _WORKSPACE_OPTION] = None,
) -> None:
    """Show build and workspace status.

    Takes ``--workspace`` like every other command: the one command whose job is
    describing a workspace has to be pointable at the workspace in question.

    Unlike the maintenance commands it does not refuse an uninitialized path —
    reporting that is the answer it exists to give — and it creates nothing.
    """
    from ferumind.core.format import SUPPORTED_FORMAT, read_format
    from ferumind.core.paths import WorkspaceRoot
    from ferumind.core.registry import list_entries

    resolved = _workspace_root(workspace)
    typer.echo(f"Workspace: {resolved}")
    typer.echo(f"Supported format: {SUPPORTED_FORMAT}")
    if resolved.is_dir():
        found = read_format(WorkspaceRoot(resolved))
        unknown = "missing (not an initialized workspace)"
        typer.echo(f"Workspace format: {found if found is not None else unknown}")
        entries = list_entries(WorkspaceRoot(resolved))
        typer.echo(f"Projects: {', '.join(e.key for e in entries) or '(none)'}")
    else:
        typer.echo("Workspace not initialized (run scripts/bootstrap_workspace.py).")


@mcp_app.command("serve")
def mcp_serve(
    workspace: Annotated[Path | None, _WORKSPACE_OPTION] = None,
) -> None:
    """Run the Ferumind MCP server on stdio."""
    from ferumind.mcp.server import serve

    serve(workspace_path=workspace, transport="stdio")


@app.command()
def migrate(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the migration plan without writing")
    ] = False,
    workspace: Annotated[Path | None, _WORKSPACE_OPTION] = None,
) -> None:
    """Migrate the workspace format to this build's supported version.

    Explicit and human-triggered (spec-versioning §1.3): creates a full
    tarball backup and a global snapshot before touching anything.
    """
    from ferumind.core.errors import FormatUnsupportedError, MigrationPrerequisiteError
    from ferumind.core.migrate import run_migration
    from ferumind.core.paths import WorkspaceRoot
    from ferumind.db.database import Database

    ws = WorkspaceRoot(_initialized_workspace_root(workspace))
    database = Database(_database_path(ws))
    database.init_schema()
    conn = database.get_connection()
    try:
        report = run_migration(conn, ws, dry_run=dry_run)
    except MigrationPrerequisiteError as exc:
        # Nothing was written: preflight runs before the backup exists. Say so,
        # so the operator does not go looking for a workspace to restore.
        typer.echo(f"Cannot migrate: {exc}", err=True)
        typer.echo(
            "Nothing was changed. Finish preparing the workspace and run migrate again.",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    except FormatUnsupportedError as exc:
        typer.echo(f"Cannot migrate: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    plan = report.plan
    if not plan.steps:
        typer.echo(f"Workspace already at format {plan.to_format}; nothing to migrate.")
        return
    steps = ", ".join(f"{n} -> {n + 1}" for n in plan.steps)
    if report.dry_run:
        typer.echo(f"Would migrate format {plan.from_format} -> {plan.to_format} ({steps}).")
        return
    typer.echo(f"Migrated format {plan.from_format} -> {plan.to_format} ({steps}).")
    typer.echo(f"Backup: {report.backup_path}")
    typer.echo(f"Snapshot: {report.snapshot_id}")
    typer.echo(f"Reindexed documents: {report.reindexed_documents}")


@app.command("reindex")
def reindex(
    project: Annotated[
        str | None, typer.Option("--project", help="Project key (default: all projects)")
    ] = None,
    workspace: Annotated[Path | None, _WORKSPACE_OPTION] = None,
) -> None:
    """Rebuild the derived search index from the Markdown on disk."""
    from ferumind.core.indexer import rebuild_index
    from ferumind.core.paths import WorkspaceRoot
    from ferumind.db.database import Database

    ws = WorkspaceRoot(_initialized_workspace_root(workspace))
    keys = _project_keys(ws, project)
    database = Database(_database_path(ws))
    database.init_schema()
    conn = database.get_connection()
    try:
        result = rebuild_index(conn, ws, keys)
    finally:
        conn.close()
    typer.echo(f"Indexed {result.documents_indexed} document(s) across {len(keys)} project(s).")
    for message in result.error_messages:
        typer.echo(f"  error: {message}", err=True)


@app.command("verify-index")
def verify_index_cmd(
    project: Annotated[
        str | None, typer.Option("--project", help="Project key (default: all projects)")
    ] = None,
    fix: Annotated[
        bool,
        typer.Option(
            "--fix",
            help="Rebuild derived index rows for projects with repairable findings",
        ),
    ] = False,
    workspace: Annotated[Path | None, _WORKSPACE_OPTION] = None,
) -> None:
    """Check derived index state against Markdown on disk (read-only by default).

    Exits non-zero when any divergence is found. ``--fix`` rebuilds derived
    tables only (via ``rebuild_index``); it never writes Markdown and never
    mutates operations, snapshots, or observation history. Against a live
    workspace, ask the owner before passing ``--fix``.
    """
    from ferumind.core.paths import WorkspaceRoot
    from ferumind.core.verify_index import verify_and_maybe_repair
    from ferumind.db.database import Database

    ws = WorkspaceRoot(_initialized_workspace_root(workspace))
    keys = _project_keys(ws, project)
    database = Database(_database_path(ws))
    if fix:
        database.init_schema()
        conn = database.get_connection()
    elif not database.db_path.is_file():
        # Fresh bootstrap has no SQLite yet. With no projects that is vacuously
        # clean; with projects the index is missing and needs reindex/--fix.
        if keys:
            typer.echo(
                "Cannot verify the index: the workspace database does not exist "
                "yet. Run ferumind reindex, or pass --fix to create and rebuild "
                "derived state.",
                err=True,
            )
            raise typer.Exit(code=1)
        typer.echo(f"Index clean: 0 document(s) across {len(keys)} project(s).")
        return
    else:
        import sqlite3

        try:
            conn = database.get_readonly_connection()
        except (OSError, sqlite3.Error) as exc:
            typer.echo(
                f"Cannot open the workspace database read-only: {exc}",
                err=True,
            )
            raise typer.Exit(code=1) from exc
    try:
        report = verify_and_maybe_repair(conn, ws, keys, fix=fix)
    finally:
        conn.close()

    if report.repaired_projects:
        typer.echo(
            f"Repaired derived index for {', '.join(report.repaired_projects)} "
            f"({report.repair_documents_indexed} document(s) reindexed)."
        )
        for message in report.repair_errors:
            typer.echo(f"  repair error: {message}", err=True)

    if report.ok:
        typer.echo(
            f"Index clean: {report.documents_checked} document(s) across "
            f"{len(report.projects_checked)} project(s)."
        )
        return

    typer.echo(
        f"Index diverged: {len(report.findings)} finding(s) across "
        f"{report.documents_checked} document(s).",
        err=True,
    )
    for finding in report.findings:
        scope = finding.project_key or "workspace"
        where = f"{scope}:{finding.path}" if finding.path else scope
        typer.echo(f"  [{finding.kind}] {where}: {finding.message}", err=True)
    raise typer.Exit(code=1)


@app.command("lint")
def lint_workspace_cmd(
    project: Annotated[
        str | None, typer.Option("--project", help="Project key (default: all projects)")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit a deterministic structured JSON report")
    ] = False,
    severity: Annotated[
        Literal["error", "warning", "info"],
        typer.Option(
            "--severity",
            help="Minimum reported severity: info (all), warning, or error",
        ),
    ] = "info",
    workspace: Annotated[Path | None, _WORKSPACE_OPTION] = None,
) -> None:
    """Report mechanical workspace problems without editing Markdown.

    Lint may reconcile ordinary out-of-band changes into the derived SQLite
    index and operation log before checking index consistency. It never writes
    document bytes and offers no automatic repair.
    """
    import json

    from ferumind.core.lint import lint_newer_format_report, lint_workspace
    from ferumind.core.paths import WorkspaceRoot
    from ferumind.db.database import Database

    ws = WorkspaceRoot(_initialized_workspace_root(workspace))
    report = lint_newer_format_report(ws)
    if report is None:
        keys = _project_keys(ws, project)
        database = Database(_database_path(ws))
        database.init_schema()
        conn = database.get_connection()
        try:
            report = lint_workspace(conn, ws, keys)
        finally:
            conn.close()
    # A newer-format preflight intentionally reaches here without registry,
    # project, or database access, matching the locked all-access refusal.

    rendered = report.at_or_above(severity)
    if json_output:
        typer.echo(
            json.dumps(
                rendered.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _echo_lint_report(rendered)
    if report.has_errors:
        raise typer.Exit(code=1)


def _echo_lint_report(report: LintReport) -> None:
    """Render the typed lint report without importing its engine at startup."""
    typer.echo(
        f"Lint checked {report.documents_checked} document(s) and "
        f"{report.links_checked} link(s) across {len(report.projects_checked)} project(s)."
    )
    for finding in report.findings:
        location = finding.project
        if finding.path is not None:
            location += f":{finding.path}"
        if finding.line is not None:
            location += f":{finding.line}"
        typer.echo(f"{finding.severity.upper()} [{finding.check_id}] {location}: {finding.message}")
    typer.echo(
        f"Summary: {report.summary.errors} error(s), "
        f"{report.summary.warnings} warning(s), {report.summary.infos} info finding(s)."
    )


@app.command("compress-images")
def compress_images(
    project: Annotated[
        str | None, typer.Option("--project", help="Project key (default: all projects)")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would change without writing")
    ] = False,
    max_edge: Annotated[
        int | None,
        typer.Option("--max-edge", help="Override the configured longest edge, in pixels"),
    ] = None,
    quality: Annotated[
        int | None,
        typer.Option("--quality", help="Override the configured JPEG/WebP quality"),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", help="List every file, not just the summary")
    ] = False,
    workspace: Annotated[Path | None, _WORKSPACE_OPTION] = None,
) -> None:
    """Re-apply the image storage policy to files already in the workspace.

    Uploads are normalized as they arrive; this brings existing files in line
    after a policy change. Each rewrite is snapshotted and logged, so a run is
    reversible, and the pass is idempotent — running it twice changes nothing
    the second time.
    """
    from ferumind.core.image_maintenance import compress_project_images
    from ferumind.core.paths import WorkspaceRoot
    from ferumind.db.database import Database

    ws = WorkspaceRoot(_initialized_workspace_root(workspace))
    policy = _storage_policy(workspace, max_edge=max_edge, quality=quality)
    keys = _project_keys(ws, project)

    database = Database(_database_path(ws))
    database.init_schema()
    conn = database.get_connection()
    total_before = total_after = total_changed = total_failed = 0
    try:
        for key in keys:
            report = compress_project_images(conn, ws, key, policy=policy, dry_run=dry_run)
            total_before += report.bytes_before
            total_after += report.bytes_after
            total_changed += report.changed
            total_failed += report.failed
            if report.scanned:
                typer.echo(
                    f"{key}: {report.changed} changed, {report.skipped} unchanged, "
                    f"{report.failed} failed "
                    f"({report.bytes_before / 1048576:.1f} MB -> "
                    f"{report.bytes_after / 1048576:.1f} MB)"
                )
            for entry in report.entries:
                if entry.error is not None:
                    typer.echo(f"  error: {entry.path}: {entry.error}", err=True)
                elif verbose and entry.changed:
                    typer.echo(
                        f"  {entry.path}: {entry.before_bytes / 1024:.0f} KB -> "
                        f"{entry.after_bytes / 1024:.0f} KB"
                    )
                elif verbose:
                    typer.echo(f"  {entry.path}: unchanged ({entry.reason})")
    finally:
        conn.close()

    prefix = "Would reclaim" if dry_run else "Reclaimed"
    typer.echo(
        f"\npolicy: max_edge={policy.max_edge} quality={policy.jpeg_quality}\n"
        f"{total_changed} file(s) rewritten across {len(keys)} project(s). "
        f"{prefix} {(total_before - total_after) / 1048576:.1f} MB "
        f"({total_before / 1048576:.1f} MB -> {total_after / 1048576:.1f} MB)."
    )
    if total_failed:
        typer.echo(f"{total_failed} file(s) failed; see errors above.", err=True)
        raise typer.Exit(code=1)


@project_app.command("create")
def project_create(
    key: Annotated[str, typer.Argument(help="Project key, e.g. 'notes'")],
    title: Annotated[str, typer.Option("--title", help="Human-readable project title")],
    workspace: Annotated[Path | None, _WORKSPACE_OPTION] = None,
) -> None:
    """Create a project: registry entry, folder skeleton, seeded spine and rules.

    Calls the same ``core.project_writes.create_project`` as the MCP ``create_project``
    tool, so a project made here is indistinguishable from one an agent made —
    snapshot-protected and operation-logged either way.

    Exists so a first project does not require a configured MCP client, and so
    projects can be created by hand without a chat session.
    """
    from ferumind.core.errors import FerumindError
    from ferumind.core.paths import PathSafetyError, WorkspaceRoot
    from ferumind.core.project_writes import create_project
    from ferumind.db.database import Database

    ws = WorkspaceRoot(_workspace_root(workspace))
    database = Database(_database_path(ws))
    database.init_schema()
    conn = database.get_connection()
    try:
        result = create_project(conn, ws, key=key, title=title)
    except (FerumindError, PathSafetyError) as exc:
        typer.echo(f"Cannot create project: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(f"Created project '{result.key}' ({result.title}) at {result.path}")
    for seeded in result.seeded:
        typer.echo(f"  seeded: {seeded}")


@project_app.command("list")
def project_list(
    workspace: Annotated[Path | None, _WORKSPACE_OPTION] = None,
) -> None:
    """List every project known to the registry, the workspace folder, or the database.

    Each key is listed once, with flags showing which sources have it — so a
    project missing from one source (e.g. its folder was deleted by hand but
    rows are still indexed) is visible rather than silently deduplicated away.
    """
    from ferumind.core.paths import WorkspaceRoot
    from ferumind.core.project_admin import list_all_projects
    from ferumind.db.database import Database

    ws = WorkspaceRoot(_workspace_root(workspace))
    database = Database(_database_path(ws))
    database.init_schema()
    conn = database.get_connection()
    try:
        summaries = list_all_projects(conn, ws)
    finally:
        conn.close()

    if not summaries:
        typer.echo("No projects found.")
        return
    for summary in summaries:
        sources = ", ".join(
            label
            for label, present in (
                ("registry", summary.in_registry),
                ("folder", summary.folder_exists),
                ("database", summary.in_database),
            )
            if present
        )
        title = f" ({summary.title})" if summary.title else ""
        typer.echo(f"{summary.key}{title} — {sources}")


@project_app.command("delete")
def project_delete(
    key: Annotated[str, typer.Argument(help="Project key to delete")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt")] = False,
    workspace: Annotated[Path | None, _WORKSPACE_OPTION] = None,
) -> None:
    """Clean stale registry/database state after a project folder was removed.

    Refuses to delete a project folder containing user knowledge.
    """
    from ferumind.core.errors import FerumindError
    from ferumind.core.paths import WorkspaceRoot
    from ferumind.core.project_admin import delete_project
    from ferumind.db.database import Database

    ws = WorkspaceRoot(_workspace_root(workspace))
    if not yes:
        confirmed = typer.confirm(
            f"Clean stale registry and database state for project {key!r}? "
            "(The project folder must already be absent.)"
        )
        if not confirmed:
            typer.echo("Aborted.")
            raise typer.Exit(code=1)

    database = Database(_database_path(ws))
    database.init_schema()
    conn = database.get_connection()
    try:
        result = delete_project(conn, ws, key)
    except FerumindError as exc:
        typer.echo(f"Cannot delete project: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(f"Cleaned stale state for project {result.key!r}.")
    typer.echo(f"Registry entry removed: {result.registry_removed}")
    typer.echo(f"Folder removed: {result.folder_removed}")
    typer.echo(f"Database rows removed: {result.rows_removed}")


@compact_app.command("reseal")
def compact_reseal(
    token: Annotated[str, typer.Argument(help="Four-word compact token")],
    workspace: Annotated[Path | None, _WORKSPACE_OPTION] = None,
) -> None:
    """Accept a hand-edited compact as intentional and refresh its integrity hash."""
    from ferumind.core import compacts
    from ferumind.core.errors import FerumindError
    from ferumind.core.paths import PathSafetyError, WorkspaceRoot
    from ferumind.db.database import Database

    ws = WorkspaceRoot(_workspace_root(workspace))
    database = Database(_database_path(ws))
    database.init_schema()
    conn = database.get_connection()
    try:
        result = compacts.reseal_compact(conn, ws, token=token)
    except (FerumindError, PathSafetyError) as exc:
        typer.echo(f"Cannot reseal compact: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(f"Resealed compact {result.token}")
    typer.echo(f"Path: {result.path}")
    typer.echo(f"State: {result.state}")
    typer.echo(f"Snapshot: {result.snapshot_id}")
    typer.echo(f"Document SHA-256: {result.document_sha256}")
