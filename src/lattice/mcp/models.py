"""MCP tool envelope model and result helpers (v2: no session fields)."""

from __future__ import annotations

from collections.abc import Sequence

from mcp.types import CallToolResult, ContentBlock, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict

from lattice.core.types import JsonMapping, JsonObject


class LatticeToolEnvelope(BaseModel):
    """Standard response envelope for all Lattice MCP tools."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: JsonObject | None = None
    error_code: str | None = None
    message: str | None = None
    details: JsonObject | None = None
    project: str | None = None


def _to_json_object(data: JsonMapping | None) -> JsonObject | None:
    if data is None:
        return None
    return dict(data)


def make_result(payload: LatticeToolEnvelope, *, is_error: bool = False) -> CallToolResult:
    """Build an MCP CallToolResult from a LatticeToolEnvelope.

    Returns structured content (for clients that support it) plus serialized
    JSON text content for backward compatibility.
    """
    json_text = payload.model_dump_json(exclude_none=True)
    return CallToolResult(
        content=[TextContent(type="text", text=json_text)],
        structuredContent=payload.model_dump(exclude_none=True),
        isError=is_error,
    )


def make_success(
    data: JsonMapping | None = None,
    *,
    project: str | None = None,
) -> CallToolResult:
    """Build a successful tool result."""
    return make_result(
        LatticeToolEnvelope(ok=True, data=_to_json_object(data), project=project),
        is_error=False,
    )


def make_rich_success(
    data: JsonMapping | None,
    content: Sequence[ContentBlock],
    *,
    project: str | None = None,
) -> CallToolResult:
    """Build a success result whose content is typed MCP blocks.

    Most Lattice tools serialize their envelope into a single ``TextContent``
    (see :func:`make_result`). ``read_file`` cannot: an image has to reach
    the host as a genuine ``ImageContent`` block, and a resource has to reach
    it as a genuine ``ResourceLink``, or the host will render base64 as text
    instead of attaching a picture.

    The Lattice envelope still travels in ``structuredContent``, so
    machine-readable fields and error codes keep working unchanged. Binary
    payloads belong **only** in their typed content block — never in the
    envelope, the text blocks, or ``_meta``.
    """
    payload = LatticeToolEnvelope(ok=True, data=_to_json_object(data), project=project)
    return CallToolResult(
        content=list(content),
        structuredContent=payload.model_dump(exclude_none=True),
        isError=False,
    )


def make_error(
    error_code: str,
    message: str,
    details: JsonMapping | None = None,
    *,
    project: str | None = None,
) -> CallToolResult:
    """Build a domain error result."""
    return make_result(
        LatticeToolEnvelope(
            ok=False,
            error_code=error_code,
            message=message,
            details=_to_json_object(details),
            project=project,
        ),
        is_error=True,
    )


# Explicit edit-state vocabulary shared by proposal/apply result enrichment.
# These fields make it unambiguous to a client whether the user's Markdown
# was actually changed: a propose result is not a saved edit (spec-mcp §5.2).


def proposal_state_fields(operation_id: str, *, project: str) -> JsonObject:
    """Build the explicit pending-edit state fields for a ``propose_*`` result."""
    return {
        "operation_status": "proposed",
        "document_mutated": False,
        "requires_apply": True,
        "next_required_tool": "apply_patch",
        "completion_state": "pending_apply",
        "user_visible_state": "not_saved",
        "recommended_action": (
            "Call apply_patch with this operation_id to save the edit unless the user "
            "asked to review first."
        ),
        "next_required_arguments": {"operation_id": operation_id, "project": project},
    }


def apply_state_fields(proposal_operation_id: str, applied_operation_id: str) -> JsonObject:
    """Build the explicit saved-edit state fields for an ``apply_patch`` result."""
    return {
        "operation_status": "applied",
        "document_mutated": True,
        "requires_apply": False,
        "completion_state": "saved",
        "user_visible_state": "saved",
        "proposal_operation_id": proposal_operation_id,
        "applied_operation_id": applied_operation_id,
        "recommended_action": "Report that the document was updated.",
    }


def read_only_annotations() -> ToolAnnotations:
    return ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def proposal_annotations() -> ToolAnnotations:
    """Annotations for pending edit proposal tools.

    Proposal tools do not mutate project files. They read a Markdown
    document, compute a guarded diff, and store pending proposal metadata for
    a later explicit apply step.
    """
    return ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )


def write_annotations() -> ToolAnnotations:
    return ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )


def open_world_write_annotations() -> ToolAnnotations:
    """Annotations for mutations that fetch caller-selected remote resources."""

    return ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
