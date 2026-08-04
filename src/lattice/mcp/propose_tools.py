"""MCP proposal tools (spec-mcp §5.2): pending edits, never saved edits.

A ``propose_*`` result carries ``document_mutated=false``,
``requires_apply=true``, ``next_required_tool=apply_patch``, and a policy
echo. Proposals are bound to project + path + base hash, expire after 24 h,
and are invalidated by out-of-band edits. The server informs; agents honor.
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import Field

from lattice.core import writes
from lattice.core.edit_targets import ExactEdit, InsertAnchor
from lattice.core.errors import LatticeError
from lattice.core.paths import PathSafetyError
from lattice.core.types import JsonObject
from lattice.core.writes import ProposalResult
from lattice.mcp.models import (
    make_success,
    proposal_annotations,
    proposal_state_fields,
)
from lattice.mcp.protocols import ToolRegistrar
from lattice.mcp.tool_context import (
    error_result,
    require_database,
    require_format_gate,
    require_workspace,
    scoped_project,
)

type LatticeToolResult = CallToolResult

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
    """Register the proposal tool family."""

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
        structured_output=False,
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
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.propose_exact_replace_patch(
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
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="propose_multi_edit_patch",
        title="Propose Multi Edit Patch",
        description=(
            "Propose an atomic batch of exact-replace edits to one document (one "
            "proposal, one apply). Any failing edit aborts the whole batch."
        ),
        annotations=proposal_annotations(),
        structured_output=False,
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
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.propose_multi_edit_patch(
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
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="propose_section_patch",
        title="Propose Section Patch",
        description=(
            "Propose replacing a heading-derived section located via "
            "get_document_map. Guarded by document and section hashes."
        ),
        annotations=proposal_annotations(),
        structured_output=False,
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
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.propose_section_patch(
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
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="propose_range_patch",
        title="Propose Range Patch",
        description=(
            "Propose replacing a specific line range (covers single-line edits). "
            "Guarded by document and range hashes from a prior read."
        ),
        annotations=proposal_annotations(),
        structured_output=False,
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
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.propose_range_patch(
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
        except (LatticeError, PathSafetyError) as exc:
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
        structured_output=False,
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
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.propose_search_replace_patch(
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
        except (LatticeError, PathSafetyError) as exc:
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
        structured_output=False,
    )
    def propose_insert_patch_tool(
        project: Annotated[str, _PROJECT_FIELD],
        path: Annotated[str, _PATH_FIELD],
        anchor: Annotated[InsertAnchor, Field(description="Where to insert relative to an anchor")],
        content: Annotated[str, Field(description="Content to insert")],
        expected_document_sha256: Annotated[
            str, Field(description="Document hash from a prior read")
        ],
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.propose_insert_patch(
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
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="propose_frontmatter_patch",
        title="Propose Frontmatter Patch",
        description=(
            "Propose setting/removing individual frontmatter keys. Identity keys "
            "(id/type/project/created) and the automatic updated are protected."
        ),
        annotations=proposal_annotations(),
        structured_output=False,
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
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.propose_frontmatter_patch(
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
        except (LatticeError, PathSafetyError) as exc:
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
        structured_output=False,
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
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.propose_patch(
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
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)

    @mcp.tool(
        name="discard_patch",
        title="Discard Patch",
        description=(
            "Withdraw a pending patch proposal so it can no longer be applied. "
            "Only operation-log metadata changes; user Markdown is untouched."
        ),
        annotations=proposal_annotations(),
        structured_output=False,
    )
    def discard_patch_tool(
        project: Annotated[str, _PROJECT_FIELD],
        operation_id: Annotated[str, Field(description="Operation id from any propose_* tool")],
    ) -> LatticeToolResult:
        try:
            require_format_gate().check_write()
            entry = scoped_project(project)
            db = require_database()
            conn = db.get_connection()
            try:
                result = writes.discard_patch(
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
        except (LatticeError, PathSafetyError) as exc:
            return error_result(exc, project=project)
