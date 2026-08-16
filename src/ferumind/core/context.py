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
from ferumind.core.format import read_format
from ferumind.core.paths import PathSafetyError, WorkspaceRoot, contained_path
from ferumind.core.reconcile import reconcile_project
from ferumind.core.registry import ProjectEntry
from ferumind.core.response_limits import ResponseBudget
from ferumind.core.skills import list_skills
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
    """One entry in the document map: what it is for, and what reading it costs.

    ``description`` and ``size_bytes`` together are the cheap first navigation
    layer — purpose and read cost for every document in the project, so an
    agent can choose a target without retrieving anything. Structure is
    ``get_document_map``'s job, not this one's.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    title: str
    description: str
    folder: str
    status: str
    edit_policy: str
    updated: str
    size_bytes: int


class ContextSkill(BaseModel):
    """One Ferumind skill index entry: its name and when to reach for it.

    The body is deliberately absent. Skills are on-demand procedures (product
    D7): the trigger is cheap enough to carry on every call, a full procedure
    is not, and ``read_skill`` fetches the body when the trigger matches.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    path: str


class ContextPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: int | None
    rules_bytes: int
    spine_bytes: int
    documents_count: int
    #: What the skills index costs on every call. Separate from ``rules_bytes``
    #: because it is a distinct claimant on the same uncapped budget, and the
    #: per-skill cost is what decides whether another skill can be afforded.
    skills_bytes: int
    #: What the document map's descriptions cost, separately from the map
    #: itself. Descriptions are the one part of this payload that grows with
    #: both the project's size and how much its agents write, so the cap
    #: decision spec-mcp §4 parks on telemetry needs its own number for them.
    descriptions_bytes: int


class ProjectContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ContextProject
    rules: ContextRules
    spine: ContextSpine | None
    spine_missing: bool
    documents: list[ContextDocument]
    skills: list[ContextSkill]
    inbox_count: int
    payload: ContextPayload


def _collect_rules(
    workspace_root: WorkspaceRoot,
    project_key: str,
    budget: ResponseBudget,
) -> ContextRules:
    """Concatenate workspace then project rules, each prefixed by a source header.

    Charged file by file from the stat, before any of them is read. Rules are
    the one part of this payload with no bound of its own — ``rules/`` is a
    creatable folder, each document may reach the 5 MiB write cap, and nothing
    limits how many there are, so repeated small appends can grow the set past
    any transport ceiling using individually tiny tool calls.
    """
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
            budget.charge(
                safe_entry.stat().st_size,
                source=f"the rules document {source}",
                remedy=(
                    "Rules are never truncated, so this project cannot start a "
                    "chat until the rules shrink. Split or trim the files under "
                    "rules/, or move reference material into a canvas the agent "
                    "reads on demand."
                ),
            )
            sources.append(source)
            parts.append(f"## {source}\n\n{safe_entry.read_text(encoding='utf-8').strip()}\n")
    return ContextRules(content_markdown="\n".join(parts), sources=sources)


def _map_bytes(documents: list[ContextDocument]) -> int:
    """Measure what the document map costs on the wire, fields included."""
    return sum(
        len(
            (
                f"{doc.path}{doc.title}{doc.description}{doc.folder}"
                f"{doc.status}{doc.edit_policy}{doc.updated}"
            ).encode()
        )
        for doc in documents
    )


def build_context(
    conn: DbConnection,
    workspace_root: WorkspaceRoot,
    entry: ProjectEntry,
    *,
    max_response_bytes: int | None = None,
) -> ProjectContext:
    """Assemble the full context payload for a project.

    *max_response_bytes* does **not** cap the payload. Uncapped is a locked
    product decision (spec-mcp §4) and stands: nothing here truncates, pages,
    or drops a rule. What it does is refuse, with a machine-readable error
    naming the contributor, when the assembled result provably could not
    reach the caller anyway.

    The distinction matters most on this call. ``get_context`` is the one the
    bootstrap instructs every new chat to make first, so a payload past the
    transport ceiling does not degrade one call — every chat in the project
    dies on its first, and on a tunnel takes the transport down with it. An
    error that names the offending rules document is the difference between
    an operator who knows to split a file and an operator filing a bug about
    a connection that keeps dropping.
    """
    project_key = entry.key
    reconcile_project(conn, workspace_root, project_key)

    budget = ResponseBudget(max_response_bytes, surface="get_context")
    rules = _collect_rules(workspace_root, project_key, budget)

    spine: ContextSpine | None = None
    project_root = contained_path(workspace_root, f"projects/{project_key}")
    spine_path = contained_path(project_root, SPINE_FILENAME)
    if spine_path.is_file():
        budget.charge(
            spine_path.stat().st_size,
            source=f"the spine {SPINE_FILENAME}",
            remedy=(
                "The spine is served whole on every contract call. Move detail "
                "out of it into canvases and leave the spine as the map."
            ),
        )
        spine_content = spine_path.read_text(encoding="utf-8")
        spine = ContextSpine(
            path=SPINE_FILENAME,
            content_markdown=spine_content,
            document_sha256=compute_sha256(spine_content),
        )

    rows = conn.execute(
        """SELECT path, title, description, folder, status, edit_policy,
                  updated_at, size_bytes
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
            description=row["description"],
            folder=row["folder"],
            status=row["status"],
            edit_policy=row["edit_policy"],
            updated=row["updated_at"],
            # Reused from the derived row the indexer already maintains from
            # the file stat, not a fresh stat per document.
            size_bytes=row["size_bytes"],
        )
        for row in rows
    ]

    inbox_row = conn.execute(
        "SELECT COUNT(*) AS n FROM documents WHERE project_key = ? AND folder = 'inbox'",
        (project_key,),
    ).fetchone()
    inbox_count = int(inbox_row["n"])

    skills = [
        ContextSkill(name=skill.name, description=skill.description, path=skill.path)
        for skill in list_skills(workspace_root)
    ]

    spine_bytes = len(spine.content_markdown.encode("utf-8")) if spine is not None else 0
    payload = ContextPayload(
        # Reads deliberately remain available for older and unmarked
        # workspaces. Echo what this workspace actually declares rather than
        # the build's supported version; ``None`` preserves the distinction
        # between an unreadable marker and an invented legacy format.
        format=read_format(workspace_root),
        rules_bytes=len(rules.content_markdown.encode("utf-8")),
        spine_bytes=spine_bytes,
        documents_count=len(documents),
        skills_bytes=sum(
            len(f"{skill.name}{skill.description}{skill.path}".encode()) for skill in skills
        ),
        descriptions_bytes=sum(len(doc.description.encode("utf-8")) for doc in documents),
    )

    # The map and the skills index are charged last, together, because
    # neither is a file the operator can point at: they are per-row costs
    # that grow with how many documents a project has. Descriptions are
    # bounded per document by MAX_DESCRIPTION_CHARS, the row count is not,
    # so this is the contributor that arrives without anyone adding anything
    # large.
    budget.charge(
        _map_bytes(documents) + payload.skills_bytes,
        source=f"the document map ({len(documents)} documents) and the skills index",
        remedy=(
            "Archive documents that are no longer live, or split the project. "
            "Everything under archive/ is already excluded from this map."
        ),
    )

    return ProjectContext(
        project=ContextProject(key=entry.key, title=entry.title, status=entry.status),
        rules=rules,
        spine=spine,
        spine_missing=spine is None,
        documents=documents,
        skills=skills,
        inbox_count=inbox_count,
        payload=payload,
    )
