"""MCP propose → apply tools (spec-mcp §5.2): the guarded edit transaction.

A ``propose_*`` result carries ``document_mutated=false``,
``requires_apply=true``, ``next_required_tool=apply_patch``, and a policy
echo. Proposals are bound to project + path + base hash, expire after 24 h,
and are invalidated by out-of-band edits. The server informs; agents honor.

``apply_patch`` is registered here rather than with the other content-mutating
tools: it is the second half of one transaction, it is the only caller of
:mod:`ferumind.core.patch_writes`'s apply path, and a reviewer reading the
guards a proposal records should see the code that revalidates them without
changing files.
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import Field

from ferumind.core import patch_writes
from ferumind.core.edit_targets import ExactEdit, InsertAnchor
from ferumind.core.errors import FerumindError
from ferumind.core.patch_writes import ProposalResult
from ferumind.core.paths import PathSafetyError
from ferumind.core.types import JsonObject
from ferumind.mcp.models import (
    FerumindResult,
    apply_state_fields,
    make_success,
    proposal_annotations,
    proposal_state_fields,
    write_annotations,
    write_result_data,
)
from ferumind.mcp.protocols import ToolRegistrar
from ferumind.mcp.result_models import ApplyPatchData, DiscardPatchData, ProposalData
from ferumind.mcp.tool_context import (
    error_result,
    require_database,
    require_format_gate,
    require_workspace,
    scoped_project,
)

_PROJECT_FIELD = Field(description="Project key; validated against the registry, never an override")
_PATH_FIELD = Field(description="Project-relative Markdown path")


def _proposal_data(result: ProposalResult) -> JsonObject:
    data: JsonObject = {
        "operation_id": result.operation_id,
        "path": result.path,
        "folder": result.folder,
        "proposal_kind": result.proposal_kind,
        "document_before_sha256": result.document_before_sha256,
        "target_before_sha256": result.target_before_sha256,
        "after_sha256": result.after_sha256,
        "diff": result.diff,
        "expires_at": result.expires_at,
        "deduped": result.deduped,
        "policy": {
            "edit_policy": result.policy.edit_policy,
            "status": result.policy.status,
            "policy_note": result.policy.policy_note,
        },
    }
    data.update(proposal_state_fields(result.operation_id, project=result.project_key))
    return data


def register_propose_tools(mcp: ToolRegistrar) -> None:
    """Register the propose → apply tool family."""

    @mcp.tool(
        name="propose_exact_replace_patch",
        title="Propose Exact Replace Patch",
        description=(
            "Preferred edit tool: propose replacing an exact (possibly multi-line) "
            "occurrence of old_string with new_string. The matched text is the "
            "guard; expected_document_sha256 is optional extra safety. Creates a "
            "pending patch — call apply_patch to save."
        ),
        annotations=proposal_annotations(),
    )
    def propose_exact_replace_patch_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, _PATH_FIELD],
        old_string: Annotated[
            str,
            Field(
                description=(
                    "Exact current text to replace (verbatim, multi-line allowed; "
                    "case- and whitespace-sensitive)"
                ),
                min_length=1,
            ),
        ],
        new_string: Annotated[str, Field(description="Replacement text")],
        occurrence: Annotated[
            int | Literal["all"] | None,
            Field(
                description=(
                    "Which occurrence to replace when old_string matches more than "
                    "once; 'all' requires expected_match_count"
                )
            ),
        ] = None,
        expected_match_count: Annotated[
            int | None, Field(description="Expected total match count (required for 'all')", ge=1)
        ] = None,
        expected_document_sha256: Annotated[
            str | None, Field(description="Optional document hash guard from a prior read")
        ] = None,
    ) -> Annotated[CallToolResult, FerumindResult[ProposalData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = patch_writes.propose_exact_replace_patch(
                    conn,
                    require_workspace(),
                    entry.key,
                    path=path,
                    old_string=old_string,
                    new_string=new_string,
                    occurrence=occurrence,
                    expected_match_count=expected_match_count,
                    expected_document_sha256=expected_document_sha256,
                )
            finally:
                conn.close()
            return make_success(_proposal_data(result), project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="propose_multi_edit_patch",
        title="Propose Multi Edit Patch",
        description=(
            "Propose an atomic batch of exact-replace edits to one document (one "
            "proposal, one apply). Any failing edit aborts the whole batch."
        ),
        annotations=proposal_annotations(),
    )
    def propose_multi_edit_patch_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, _PATH_FIELD],
        edits: Annotated[
            list[ExactEdit],
            Field(
                description=(
                    "Edits applied in order, each against the result of the previous "
                    "(old_string, new_string, optional occurrence)"
                ),
                min_length=1,
            ),
        ],
        expected_document_sha256: Annotated[
            str | None, Field(description="Optional document hash guard from a prior read")
        ] = None,
    ) -> Annotated[CallToolResult, FerumindResult[ProposalData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = patch_writes.propose_multi_edit_patch(
                    conn,
                    require_workspace(),
                    entry.key,
                    path=path,
                    edits=edits,
                    expected_document_sha256=expected_document_sha256,
                )
            finally:
                conn.close()
            return make_success(_proposal_data(result), project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="propose_section_patch",
        title="Propose Section Patch",
        description=(
            "Propose replacing a heading-derived section located via "
            "get_document_map. Guarded by document and section hashes."
        ),
        annotations=proposal_annotations(),
    )
    def propose_section_patch_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, _PATH_FIELD],
        section_id: Annotated[str, Field(description="section_id from get_document_map")],
        expected_document_sha256: Annotated[
            str, Field(description="Document hash from get_document_map")
        ],
        expected_section_sha256: Annotated[
            str, Field(description="Section content hash from get_document_map")
        ],
        new_content: Annotated[str, Field(description="Replacement section content")],
    ) -> Annotated[CallToolResult, FerumindResult[ProposalData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = patch_writes.propose_section_patch(
                    conn,
                    require_workspace(),
                    entry.key,
                    path=path,
                    section_id=section_id,
                    expected_document_sha256=expected_document_sha256,
                    expected_section_sha256=expected_section_sha256,
                    new_content=new_content,
                )
            finally:
                conn.close()
            return make_success(_proposal_data(result), project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="propose_range_patch",
        title="Propose Range Patch",
        description=(
            "Propose replacing a specific line range (covers single-line edits). "
            "Guarded by document and range hashes from a prior read."
        ),
        annotations=proposal_annotations(),
    )
    def propose_range_patch_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, _PATH_FIELD],
        start_line: Annotated[int, Field(description="First line (1-indexed)", ge=1)],
        end_line: Annotated[int, Field(description="Last line (inclusive)", ge=1)],
        expected_document_sha256: Annotated[
            str, Field(description="Document hash from a prior read")
        ],
        expected_range_sha256: Annotated[
            str, Field(description="Range hash from read_document_range")
        ],
        new_content: Annotated[str, Field(description="Replacement content for the range")],
    ) -> Annotated[CallToolResult, FerumindResult[ProposalData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = patch_writes.propose_range_patch(
                    conn,
                    require_workspace(),
                    entry.key,
                    path=path,
                    start_line=start_line,
                    end_line=end_line,
                    expected_document_sha256=expected_document_sha256,
                    expected_range_sha256=expected_range_sha256,
                    new_content=new_content,
                )
            finally:
                conn.close()
            return make_success(_proposal_data(result), project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="propose_search_replace_patch",
        title="Propose Search Replace Patch",
        description=(
            "Propose replacing a single-line literal/regex match (or a controlled "
            "set with occurrence='all' + expected_match_count). Guarded by the "
            "document hash."
        ),
        annotations=proposal_annotations(),
    )
    def propose_search_replace_patch_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, _PATH_FIELD],
        find: Annotated[str, Field(description="Text or regex to find", min_length=1)],
        replace: Annotated[str, Field(description="Replacement text")],
        expected_document_sha256: Annotated[
            str, Field(description="Document hash from a prior read")
        ],
        mode: Annotated[Literal["literal", "regex"], Field(description="Match mode")] = "literal",
        case_sensitive: Annotated[bool, Field(description="Case-sensitive matching")] = False,
        occurrence: Annotated[
            int | Literal["all"],
            Field(description="Occurrence to replace, or 'all' with expected_match_count"),
        ] = 1,
        expected_match_count: Annotated[
            int | None, Field(description="Expected total match count", ge=1)
        ] = None,
        include_code_blocks: Annotated[
            bool, Field(description="Match inside fenced code blocks")
        ] = True,
    ) -> Annotated[CallToolResult, FerumindResult[ProposalData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = patch_writes.propose_search_replace_patch(
                    conn,
                    require_workspace(),
                    entry.key,
                    path=path,
                    find=find,
                    replace=replace,
                    mode=mode,
                    case_sensitive=case_sensitive,
                    occurrence=occurrence,
                    expected_document_sha256=expected_document_sha256,
                    expected_match_count=expected_match_count,
                    include_code_blocks=include_code_blocks,
                )
            finally:
                conn.close()
            return make_success(_proposal_data(result), project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="propose_insert_patch",
        title="Propose Insert Patch",
        description=(
            "Propose inserting content before/after a safe anchor (line, section, "
            "match, or end of file). Guarded by the document hash and optional "
            "anchor hash. The right tool for append-only logs."
        ),
        annotations=proposal_annotations(),
    )
    def propose_insert_patch_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, _PATH_FIELD],
        anchor: Annotated[InsertAnchor, Field(description="Where to insert relative to an anchor")],
        content: Annotated[str, Field(description="Content to insert")],
        expected_document_sha256: Annotated[
            str, Field(description="Document hash from a prior read")
        ],
    ) -> Annotated[CallToolResult, FerumindResult[ProposalData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = patch_writes.propose_insert_patch(
                    conn,
                    require_workspace(),
                    entry.key,
                    path=path,
                    anchor=anchor,
                    content=content,
                    expected_document_sha256=expected_document_sha256,
                )
            finally:
                conn.close()
            return make_success(_proposal_data(result), project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="propose_frontmatter_patch",
        title="Propose Frontmatter Patch",
        description=(
            "Propose setting/removing individual frontmatter keys. Identity keys "
            "(id/type/project/created) and the automatic updated are protected."
        ),
        annotations=proposal_annotations(),
    )
    def propose_frontmatter_patch_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, _PATH_FIELD],
        set_values: Annotated[
            JsonObject | None,
            Field(description="Frontmatter keys to set (e.g. status, edit_policy, title)"),
        ] = None,
        remove_keys: Annotated[
            list[str] | None, Field(description="Frontmatter keys to remove")
        ] = None,
        expected_document_sha256: Annotated[
            str | None, Field(description="Optional document hash guard from a prior read")
        ] = None,
    ) -> Annotated[CallToolResult, FerumindResult[ProposalData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = patch_writes.propose_frontmatter_patch(
                    conn,
                    require_workspace(),
                    entry.key,
                    path=path,
                    set_values=set_values or {},
                    remove_keys=remove_keys or [],
                    expected_document_sha256=expected_document_sha256,
                )
            finally:
                conn.close()
            return make_success(_proposal_data(result), project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="propose_patch",
        title="Propose Patch",
        description=(
            "Coarse fallback patch. mode=body replaces only the Markdown body "
            "(frontmatter preserved); mode=full replaces the whole file and must "
            "keep valid managed frontmatter. Prefer the targeted propose_* tools."
        ),
        annotations=proposal_annotations(),
    )
    def propose_patch_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, _PATH_FIELD],
        new_content: Annotated[str, Field(description="Replacement content per mode")],
        mode: Annotated[
            Literal["body", "full"],
            Field(description="body: replace document body only; full: replace entire file"),
        ] = "body",
        expected_document_sha256: Annotated[
            str | None, Field(description="Optional document hash guard from a prior read")
        ] = None,
    ) -> Annotated[CallToolResult, FerumindResult[ProposalData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = patch_writes.propose_patch(
                    conn,
                    require_workspace(),
                    entry.key,
                    path=path,
                    new_content=new_content,
                    mode=mode,
                    expected_document_sha256=expected_document_sha256,
                )
            finally:
                conn.close()
            return make_success(_proposal_data(result), project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="discard_patch",
        title="Discard Patch",
        description=(
            "Withdraw a pending patch proposal so it can no longer be applied. Only "
            "operation-log metadata changes; user Markdown is untouched."
        ),
        annotations=proposal_annotations(),
    )
    def discard_patch_tool(
        project: Annotated[str, _PROJECT_FIELD],
        operation_id: Annotated[str, Field(description="Operation id from any propose_* tool")],
    ) -> Annotated[CallToolResult, FerumindResult[DiscardPatchData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = patch_writes.discard_patch(
                    conn,
                    require_workspace(),
                    entry.key,
                    operation_id,
                )
            finally:
                conn.close()
            return make_success(
                {
                    "operation_id": result.operation_id,
                    "path": result.path,
                    "state": result.state,
                    "document_mutated": False,
                },
                project=entry.key,
            )
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="apply_patch",
        title="Apply Patch",
        description=(
            "Apply a previously proposed patch by operation_id. Revalidates the "
            "proposal binding, hash guards, and 24 h TTL; snapshots before "
            "writing. Returns the new document_sha256 — chain it into the next "
            "edit's expected_document_sha256 instead of re-reading."
        ),
        annotations=write_annotations(),
    )
    def apply_patch_tool(
        project: Annotated[str, _PROJECT_FIELD],
        operation_id: Annotated[str, Field(description="Operation id from a propose_* tool")],
    ) -> Annotated[CallToolResult, FerumindResult[ApplyPatchData]]:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = patch_writes.apply_patch(
                    conn, require_workspace(), entry.key, operation_id
                )
            finally:
                conn.close()
            data = write_result_data(result)
            data["diff"] = result.diff
            data.update(apply_state_fields(operation_id, result.operation_id))
            return make_success(data, project=entry.key)
        except (FerumindError, PathSafetyError) as exc:
            return error_result(exc, project=project)
