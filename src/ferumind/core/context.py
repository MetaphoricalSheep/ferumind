"""``get_context`` assembly: the contract call (00 D9, spec-mcp §4).

Returns the merged workspace + project rules (concatenation with source
headers — no semantic merging), the spine, and the document map, uncapped,
with payload-size telemetry. Reconciles the project against the disk first
so the map never serves stale rows.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ferumind.core.documents import compute_sha256
from ferumind.core.folders import SPINE_FILENAME
from ferumind.core.format import SUPPORTED_FORMAT
from ferumind.core.paths import PathSafetyError, WorkspaceRoot, contained_path
from ferumind.core.reconcile import reconcile_project
from ferumind.core.registry import ProjectEntry
from ferumind.core.types import DbConnection


class ContextProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    status: str


class ContextRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_markdown: str
    sources: list[str]


class ContextSpine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content_markdown: str
    document_sha256: str


class ContextDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    title: str
    folder: str
    status: str
    edit_policy: str
    updated: str


class ContextPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: int
    rules_bytes: int
    spine_bytes: int
    documents_count: int


class ProjectContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ContextProject
    rules: ContextRules
    spine: ContextSpine | None
    spine_missing: bool
    documents: list[ContextDocument]
    inbox_count: int
    payload: ContextPayload


def _collect_rules(workspace_root: WorkspaceRoot, project_key: str) -> ContextRules:
    """Concatenate workspace then project rules, each prefixed by a source header."""
    parts: list[str] = []
    sources: list[str] = []
    rule_dirs = [
        (contained_path(workspace_root, "system/rules"), "system/rules"),
        (
            contained_path(workspace_root, f"projects/{project_key}/rules"),
            f"projects/{project_key}/rules",
        ),
    ]
    for rules_dir, prefix in rule_dirs:
        if not rules_dir.is_dir():
            continue
        for entry in sorted(rules_dir.glob("*.md")):
            if not entry.is_file():
                continue
            try:
                safe_entry = contained_path(rules_dir, entry.name)
            except PathSafetyError:
                continue
            source = f"{prefix}/{entry.name}"
            sources.append(source)
            parts.append(f"## {source}\n\n{safe_entry.read_text(encoding='utf-8').strip()}\n")
    return ContextRules(content_markdown="\n".join(parts), sources=sources)


def build_context(
    conn: DbConnection,
    workspace_root: WorkspaceRoot,
    entry: ProjectEntry,
) -> ProjectContext:
    """Assemble the full context payload for a project."""
    project_key = entry.key
    reconcile_project(conn, workspace_root, project_key)

    rules = _collect_rules(workspace_root, project_key)

    spine: ContextSpine | None = None
    project_root = contained_path(workspace_root, f"projects/{project_key}")
    spine_path = contained_path(project_root, SPINE_FILENAME)
    if spine_path.is_file():
        spine_content = spine_path.read_text(encoding="utf-8")
        spine = ContextSpine(
            path=SPINE_FILENAME,
            content_markdown=spine_content,
            document_sha256=compute_sha256(spine_content),
        )

    rows = conn.execute(
        """SELECT path, title, folder, status, edit_policy, updated_at
           FROM documents
           WHERE project_key = ?
             AND status != 'archived'
             AND folder NOT IN ('archive', 'inbox')
           ORDER BY folder, path""",
        (project_key,),
    ).fetchall()
    documents = [
        ContextDocument(
            path=row["path"],
            title=row["title"],
            folder=row["folder"],
            status=row["status"],
            edit_policy=row["edit_policy"],
            updated=row["updated_at"],
        )
        for row in rows
    ]

    inbox_row = conn.execute(
        "SELECT COUNT(*) AS n FROM documents WHERE project_key = ? AND folder = 'inbox'",
        (project_key,),
    ).fetchone()
    inbox_count = int(inbox_row["n"])

    spine_bytes = len(spine.content_markdown.encode("utf-8")) if spine is not None else 0
    payload = ContextPayload(
        format=SUPPORTED_FORMAT,
        rules_bytes=len(rules.content_markdown.encode("utf-8")),
        spine_bytes=spine_bytes,
        documents_count=len(documents),
    )

    return ProjectContext(
        project=ContextProject(key=entry.key, title=entry.title, status=entry.status),
        rules=rules,
        spine=spine,
        spine_missing=spine is None,
        documents=documents,
        inbox_count=inbox_count,
        payload=payload,
    )
