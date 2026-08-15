"""Project creation: the registry entry, folder skeleton, and seeded documents.

This is the MCP-facing half of project administration. The operator-only half —
listing and deleting projects from the CLI — lives in
:mod:`ferumind.core.project_admin` and is deliberately outside the MCP surface;
the two must not be merged.

Creating a project is the one workspace-level write. It takes the workspace
lock rather than a project lock, and it publishes in a fixed order: snapshot
the intended transition, stage the folder skeleton and rename it into place,
commit durable bookkeeping while the project is still absent from the registry,
and only then publish the registry entry. Every step before publication is
compensated on failure, so a caller can never enter a half-built project.

The other write domains have their own modules: :mod:`ferumind.core.patch_writes`,
:mod:`ferumind.core.document_writes`, :mod:`ferumind.core.upload_writes`, and
:mod:`ferumind.core.lifecycle_writes`. Shared guards live in
:mod:`ferumind.core.write_common`, bounds in :mod:`ferumind.core.write_limits`.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ferumind.core.errors import FerumindError, FrontmatterInvalidError, PathExistsError
from ferumind.core.file_io import atomic_write_text, ensure_private_directory
from ferumind.core.folders import PROJECT_DIRECTORIES, SPINE_FILENAME
from ferumind.core.frontmatter import (
    MAX_DESCRIPTION_CHARS,
    FrontmatterBehavior,
    extract_frontmatter_block,
    generate_frontmatter,
    new_document_id,
    parse_frontmatter,
    validate_description,
)
from ferumind.core.locks import acquire_workspace_lock
from ferumind.core.operations import OP_APPLIED, record_operation
from ferumind.core.paths import WorkspaceRoot, contained_path
from ferumind.core.registry import (
    ProjectEntry,
    load_registry,
    save_registry,
    serialize_registry,
    validate_project_key,
)
from ferumind.core.snapshots import (
    create_global_snapshot,
    new_snapshot_id,
    record_snapshot_in_db,
)
from ferumind.core.types import DbConnection
from ferumind.core.write_common import reindex_after_write, validate_title

logger = logging.getLogger(__name__)


class CreateProjectResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    path: str
    operation_id: str
    snapshot_id: str
    seeded: list[str]


def create_project(
    conn: DbConnection,
    workspace_root: WorkspaceRoot,
    *,
    key: str,
    title: str,
) -> CreateProjectResult:
    """Create a project: registry entry + folder skeleton + seeded spine/rules.

    Seeds ``spine.md`` and ``rules/00-project.md`` from
    ``system/templates/`` (installed from product/contract by the bootstrap
    script), substituting ``{{project_title}}``.
    """
    project_key = validate_project_key(key)
    validate_title(title)

    with acquire_workspace_lock(workspace_root):
        registry = load_registry(workspace_root)
        if str(project_key) in registry:
            raise PathExistsError(
                f"Project {key!r} already exists",
                details={"key": key},
            )
        project_dir = contained_path(workspace_root, f"projects/{project_key}")
        if project_dir.exists():
            raise PathExistsError(f"Project directory already exists: projects/{project_key}")

        seeded = [SPINE_FILENAME, "rules/00-project.md"]
        spine_content = _seed_from_template(
            workspace_root,
            template_name="spine.md",
            project_key=str(project_key),
            title=title,
            behavior=FrontmatterBehavior(),
        )
        rules_content = _seed_from_template(
            workspace_root,
            template_name="project-rules.md",
            project_key=str(project_key),
            title=title,
            behavior=FrontmatterBehavior(edit_policy="ask-human"),
        )
        rules_rel = "rules/00-project.md"

        entry = ProjectEntry(
            key=str(project_key),
            title=title,
            path=f"projects/{project_key}",
            status="active",
        )
        updated_registry = dict(registry)
        updated_registry[str(project_key)] = entry
        registry_file = contained_path(workspace_root, "system/projects.yml")
        before_files = (
            {"system/projects.yml": registry_file.read_text(encoding="utf-8")}
            if registry_file.is_file()
            else {}
        )
        after_files = {
            f"projects/{project_key}/{SPINE_FILENAME}": spine_content,
            f"projects/{project_key}/{rules_rel}": rules_content,
            "system/projects.yml": serialize_registry(updated_registry),
        }

        # Snapshot the intended transition before publishing either the new
        # project folder or its registry entry.
        snapshot_id = new_snapshot_id()
        snapshot_dir = create_global_snapshot(
            workspace_root,
            snapshot_id=snapshot_id,
            operation_type="create_project",
            target_project_key=str(project_key),
            reason="create_project",
            before_files=before_files,
            after_files=after_files,
        )

        # `projects` belongs to the operator: created private, never re-chmodded.
        # The staging base is Ferumind's own private state, so it is forced to
        # 0700 on every touch. See ``file_io`` for the rule.
        projects_root = contained_path(workspace_root, "projects")
        ensure_private_directory(projects_root)
        staging_base = contained_path(workspace_root, ".ferumind/project-staging")
        staging_base.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging_base.chmod(0o700)
        staging_dir = contained_path(staging_base, f"{project_key}-{snapshot_id}")
        staging_dir.mkdir(mode=0o700)
        published = False
        try:
            for sub in PROJECT_DIRECTORIES:
                contained_path(staging_dir, sub).mkdir(
                    mode=0o700,
                    parents=True,
                    exist_ok=True,
                )
            atomic_write_text(contained_path(staging_dir, SPINE_FILENAME), spine_content)
            atomic_write_text(contained_path(staging_dir, rules_rel), rules_content)

            # Directory rename publishes a complete skeleton atomically.
            staging_dir.replace(project_dir)
            published = True
        except BaseException:
            if published:
                _withdraw_unpublished_project(project_dir, staging_dir)
            else:
                shutil.rmtree(staging_dir, ignore_errors=True)
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            raise

        # Durable bookkeeping is committed while the project is still absent
        # from the registry. Project-scoped callers therefore cannot enter the
        # new tree before a failure can be compensated.
        try:
            record_snapshot_in_db(
                conn,
                snapshot_id=snapshot_id,
                # Global snapshots are not readable through the project-file
                # snapshot tools; keep them out of project snapshot listings.
                project_key="",
                target_path=None,
                snapshot_dir=str(snapshot_dir),
                reason="create_project",
                commit=False,
            )
            op_id = record_operation(
                conn,
                project_key=str(project_key),
                operation_type="create_project",
                tool_name="create_project",
                request_json={"key": str(project_key), "title": title},
                snapshot_id=snapshot_id,
                state=OP_APPLIED,
                commit=False,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            if _withdraw_unpublished_project(project_dir, staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)
                shutil.rmtree(snapshot_dir, ignore_errors=True)
            raise

        # Registry publication is the visibility boundary. atomic_write_text
        # may replace the file successfully and then report a directory-fsync
        # failure, so inspect the source of truth before compensating.
        try:
            save_registry(workspace_root, updated_registry)
        except (FerumindError, OSError):
            current_registry = _load_registry_for_publication_check(workspace_root)
            if current_registry == updated_registry:
                logger.warning(
                    "Project registry publication reported failure after the intended "
                    "registry became visible; treating project creation as committed"
                )
            elif current_registry == registry:
                try:
                    conn.execute(
                        "DELETE FROM operations WHERE id = ? AND operation_type = ?",
                        (op_id, "create_project"),
                    )
                    conn.execute(
                        "DELETE FROM snapshots WHERE id = ? AND snapshot_dir = ?",
                        (snapshot_id, str(snapshot_dir)),
                    )
                    conn.commit()
                except sqlite3.Error:
                    conn.rollback()
                    logger.critical(
                        "Could not compensate hidden project bookkeeping; preserving "
                        "the unpublished project tree and snapshot"
                    )
                    raise
                if _withdraw_unpublished_project(project_dir, staging_dir):
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    shutil.rmtree(snapshot_dir, ignore_errors=True)
                raise
            else:
                logger.critical(
                    "Project registry state is ambiguous after publication failure; "
                    "preserving the project tree, snapshot, and bookkeeping"
                )
                raise
        for rel in seeded:
            reindex_after_write(conn, workspace_root, str(project_key), project_dir / rel)

    return CreateProjectResult(
        key=entry.key,
        title=entry.title,
        path=entry.path,
        operation_id=op_id,
        snapshot_id=snapshot_id,
        seeded=seeded,
    )


def _withdraw_unpublished_project(project_dir: Path, staging_dir: Path) -> bool:
    """Move a hidden project out of ``projects/`` before recursive cleanup.

    Registry publication happens only after durable bookkeeping, so a project
    reaching this helper was never visible to scoped callers. Renaming first
    also ensures cleanup never recursively deletes through a live project path.
    """
    try:
        if project_dir.exists():
            project_dir.replace(staging_dir)
        return True
    except OSError as exc:
        logger.critical(
            "Could not withdraw an unpublished project tree; preserving it (type=%s)",
            type(exc).__name__,
        )
        return False


def _load_registry_for_publication_check(
    workspace_root: WorkspaceRoot,
) -> dict[str, ProjectEntry] | None:
    """Read registry state after an ambiguous atomic publication failure."""
    try:
        return load_registry(workspace_root)
    except FerumindError as exc:
        logger.critical(
            "Could not verify project registry after publication failure (type=%s)",
            type(exc).__name__,
        )
        return None


def _seed_from_template(
    workspace_root: Path,
    *,
    template_name: str,
    project_key: str,
    title: str,
    behavior: FrontmatterBehavior,
) -> str:
    """Render a seed document from a workspace template.

    The template's own frontmatter (title/status placeholders) is replaced by
    generated managed frontmatter; the body keeps the template's guidance
    with ``{{project_title}}`` substituted.

    ``description`` comes from the template rather than from this function,
    because the template is where a human already wrote what that file shape
    is for. Missing or invalid template metadata is refused instead of being
    inferred from a title or filename.
    """
    templates_dir = contained_path(workspace_root, "system/templates")
    template_path = contained_path(templates_dir, template_name)
    if not template_path.is_file():
        raise FrontmatterInvalidError(f"Required project template {template_name!r} is missing")

    raw = template_path.read_text(encoding="utf-8")
    template_fm = parse_frontmatter(raw)
    description = _render_template_description(
        template_fm.get("description"),
        template_name=template_name,
        title=title,
    )
    _fm, body = extract_frontmatter_block(raw)
    body = body.replace("{{project_title}}", title)
    fm = generate_frontmatter(
        doc_id=new_document_id(),
        project_key=project_key,
        title=title,
        description=description,
        behavior=behavior,
    )
    return fm + body.lstrip("\n")


def _render_template_description(
    value: object,
    *,
    template_name: str,
    title: str,
) -> str:
    """Render required template metadata without making long titles invalid.

    The generic rendering is still authored by the template: only its
    ``{{project_title}}`` reference becomes "this project". It is validated
    first, so this is not a fallback for missing or malformed metadata. A
    personalized rendering is preferred when it fits the description bound.
    """
    if not isinstance(value, str):
        raise FrontmatterInvalidError(
            f"Project template {template_name!r} must define a string description"
        )

    generic = validate_description(value.replace("{{project_title}}", "this project"))
    personalized = value.replace("{{project_title}}", title).strip()
    if len(personalized) > MAX_DESCRIPTION_CHARS:
        return generic
    return validate_description(personalized)
