"""Lattice CLI.

Commands land as the phases in product/roadmap.md do; the current surface
covers serving the MCP server, workspace migration, and index maintenance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    help="Lattice — local-first, Markdown-backed knowledge workspace.",
    no_args_is_help=True,
)
mcp_app = typer.Typer(help="MCP server commands.")
compact_app = typer.Typer(help="Workspace-level compact commands.")
project_app = typer.Typer(help="Project administration commands.")
app.add_typer(mcp_app, name="mcp")
app.add_typer(compact_app, name="compact")
app.add_typer(project_app, name="project")


def _workspace_root(workspace: Path | None) -> Path:
    from lattice.core.config import load_config
    from lattice.core.paths import resolve_repo_root, resolve_workspace_root

    config = load_config(workspace)
    if config.workspace_path.is_absolute():
        return config.workspace_path.resolve()
    repo = resolve_repo_root()
    return resolve_workspace_root(repo, str(config.workspace_path))


def _database_path(workspace: Path) -> Path:
    from lattice.core.paths import contained_path

    return contained_path(workspace, ".lattice/lattice.sqlite")


_WORKSPACE_OPTION = typer.Option(
    "--workspace", help="Workspace directory (default: LATTICE_WORKSPACE or ./workspace)"
)


@app.command()
def info() -> None:
    """Show build and workspace status."""
    from lattice.core.format import SUPPORTED_FORMAT, read_format
    from lattice.core.paths import WorkspaceRoot
    from lattice.core.registry import list_entries

    workspace = _workspace_root(None)
    typer.echo(f"Workspace: {workspace}")
    typer.echo(f"Supported format: {SUPPORTED_FORMAT}")
    if workspace.is_dir():
        found = read_format(WorkspaceRoot(workspace))
        typer.echo(f"Workspace format: {found if found is not None else 'missing (pre-format)'}")
        entries = list_entries(WorkspaceRoot(workspace))
        typer.echo(f"Projects: {', '.join(e.key for e in entries) or '(none)'}")
    else:
        typer.echo("Workspace not initialized (run scripts/bootstrap_workspace.py).")


@mcp_app.command("serve")
def mcp_serve(
    workspace: Annotated[Path | None, _WORKSPACE_OPTION] = None,
) -> None:
    """Run the Lattice MCP server on stdio."""
    from lattice.mcp.server import serve

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
    from lattice.core.errors import FormatUnsupportedError
    from lattice.core.migrate import run_migration
    from lattice.core.paths import WorkspaceRoot
    from lattice.db.database import Database

    ws = WorkspaceRoot(_workspace_root(workspace))
    database = Database(_database_path(ws))
    database.init_schema()
    conn = database.get_connection()
    try:
        report = run_migration(conn, ws, dry_run=dry_run)
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
    from lattice.core.indexer import rebuild_index
    from lattice.core.paths import WorkspaceRoot
    from lattice.core.registry import list_entries, require_project
    from lattice.db.database import Database

    ws = WorkspaceRoot(_workspace_root(workspace))
    keys = (
        [require_project(ws, project).key]
        if project is not None
        else [entry.key for entry in list_entries(ws)]
    )
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
    from lattice.core.config import load_config
    from lattice.core.image_maintenance import compress_project_images
    from lattice.core.paths import WorkspaceRoot
    from lattice.core.registry import list_entries, require_project
    from lattice.db.database import Database

    ws = WorkspaceRoot(_workspace_root(workspace))
    config = load_config(workspace)
    policy = config.image_policy
    if max_edge is not None:
        policy = policy.model_copy(update={"max_edge": max_edge})
    if quality is not None:
        policy = policy.model_copy(update={"jpeg_quality": quality})
    # Validate once, up front: a bad override should fail before the first
    # file is touched rather than partway through the workspace.
    policy = policy.validated()

    keys = (
        [require_project(ws, project).key]
        if project is not None
        else [entry.key for entry in list_entries(ws)]
    )

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


@project_app.command("list")
def project_list(
    workspace: Annotated[Path | None, _WORKSPACE_OPTION] = None,
) -> None:
    """List every project known to the registry, the workspace folder, or the database.

    Each key is listed once, with flags showing which sources have it — so a
    project missing from one source (e.g. its folder was deleted by hand but
    rows are still indexed) is visible rather than silently deduplicated away.
    """
    from lattice.core.paths import WorkspaceRoot
    from lattice.core.project_admin import list_all_projects
    from lattice.db.database import Database

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
    from lattice.core.errors import LatticeError
    from lattice.core.paths import WorkspaceRoot
    from lattice.core.project_admin import delete_project
    from lattice.db.database import Database

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
    except LatticeError as exc:
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
    from lattice.core import compacts
    from lattice.core.errors import LatticeError
    from lattice.core.paths import PathSafetyError, WorkspaceRoot
    from lattice.db.database import Database

    ws = WorkspaceRoot(_workspace_root(workspace))
    database = Database(_database_path(ws))
    database.init_schema()
    conn = database.get_connection()
    try:
        result = compacts.reseal_compact(conn, ws, token=token)
    except (LatticeError, PathSafetyError) as exc:
        typer.echo(f"Cannot reseal compact: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(f"Resealed compact {result.token}")
    typer.echo(f"Path: {result.path}")
    typer.echo(f"State: {result.state}")
    typer.echo(f"Snapshot: {result.snapshot_id}")
    typer.echo(f"Document SHA-256: {result.document_sha256}")
