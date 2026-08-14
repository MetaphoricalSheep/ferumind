"""MCP tool envelope model and result helpers (no session fields)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from mcp.types import CallToolResult, ContentBlock, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict

from ferumind.core.types import JsonMapping, JsonObject
from ferumind.core.write_common import WriteResult

#: ``details`` carries open-ended machine-readable recovery data (current
#: hashes, match locations, limits), so the schema can only honestly say
#: "an object". ``Any`` is deliberate and load-bearing here: the obvious
#: spelling, ``JsonObject``, is defined recursively in ``core/types.py`` and
#: drags ``JsonValue``'s self-referential ``$defs`` into all 47 output
#: schemas — unresolvable for clients that flatten ``$ref``, and larger.
#: Construction still goes through ``JsonMapping``; only the advertised
#: schema is widened.
type ErrorDetails = dict[str, Any]


def strip_schema_titles(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively drop Pydantic's auto-generated ``title`` keys, in place.

    Pydantic titles every model and every field ("Ok", "Error Code", …). They
    are decorative — no MCP client needs one — and an ``outputSchema`` is a
    machine contract that ships on every ``tools/list``. Across the whole tool
    surface and its nested ``$defs`` the titles alone measured ~19 KB of caller
    context saying nothing the field name already says.

    Applied once at the MCP boundary rather than as a ``json_schema_extra`` on
    the payload models, because several tools advertise a **core** model
    directly (``get_context`` is :class:`~ferumind.core.context.ProjectContext`)
    and schema presentation is not core's concern. One mechanism covers both.
    """
    schema.pop("title", None)
    for key in ("properties", "$defs"):
        for value in schema.get(key, {}).values():
            if isinstance(value, dict):
                strip_schema_titles(cast("dict[str, Any]", value))
    for key in ("items", "additionalProperties"):
        nested = schema.get(key)
        if isinstance(nested, dict):
            strip_schema_titles(cast("dict[str, Any]", nested))
    for key in ("anyOf", "oneOf", "allOf"):
        for value in schema.get(key, []):
            if isinstance(value, dict):
                strip_schema_titles(cast("dict[str, Any]", value))
    return schema


class FerumindResult[TData](BaseModel):
    """The advertised output schema for every Ferumind tool.

    Generic over the success payload so each tool declares its own ``data``
    shape while sharing one envelope. Tools attach it as schema metadata::

        -> Annotated[CallToolResult, FerumindResult[ReadDocumentData]]

    The SDK derives ``outputSchema`` from the annotation and then returns the
    hand-built ``CallToolResult`` verbatim (see :func:`make_result`).

    **Every field except ``ok`` is optional, and that is the contract, not
    laziness.** The SDK validates ``structured_content`` on *every* result,
    including ``is_error=True`` ones, so this single model has to accept the
    success arm, a domain error, a sanitised ``INTERNAL_ERROR``, and a
    ``VALIDATION_ERROR`` from the tool boundary. Making ``data`` required
    would raise ``ToolError`` on every failure — and that exception's text
    quotes the rejected input, which is the leak ``tool_boundary`` exists to
    stop. ``ok`` is the discriminator.

    Flat rather than a tagged ``Literal[True]``/``Literal[False]`` union: a
    union puts ``anyOf`` at the schema root, and clients reject a root that
    is not ``type: object``.
    """

    model_config = ConfigDict(extra="forbid")

    # Deliberately undescribed. These six fields are identical on every tool,
    # so a description here is the same sentence shipped once per tool in
    # ``tools/list``. The envelope's semantics are stated once, in the server
    # ``instructions`` string, where they cost one copy.
    ok: bool
    data: TData | None = None
    error_code: str | None = None
    message: str | None = None
    details: ErrorDetails | None = None
    project: str | None = None


class FerumindToolEnvelope(BaseModel):
    """Runtime constructor for the envelope :class:`FerumindResult` describes.

    Kept separate because this one is *built* (its ``data`` is an already
    serialized ``JsonObject``) while ``FerumindResult`` is *declared* (its
    ``data`` is a per-tool model). ``test_mcp_surface.py`` asserts the two
    carry identical field names, so they cannot drift apart.
    """

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


def make_result(payload: FerumindToolEnvelope, *, is_error: bool = False) -> CallToolResult:
    """Build an MCP CallToolResult from a FerumindToolEnvelope.

    Returns structured content (for clients that support it) plus serialized
    JSON text content for backward compatibility, as the spec asks of any
    tool that returns structured content.

    How a hand-built result still advertises an ``outputSchema``
    -----------------------------------------------------------
    Tools return ``CallToolResult`` — the MCP *transport* type — because this
    function hand-builds the envelope, ``is_error``, and (for ``read_file``)
    genuine ``ImageContent`` blocks. On a bare ``-> CallToolResult``
    annotation the SDK gives up and advertises no schema at all.

    ``Annotated[CallToolResult, FerumindResult[Payload]]`` is the SDK's
    escape hatch: ``func_metadata`` derives ``outputSchema`` from the
    annotation's metadata type, and ``convert_result`` then validates
    ``structured_content`` against it and returns this result **verbatim** —
    content blocks, ``is_error`` and all. So the schema is advertised without
    the envelope being re-wrapped.

    Historical note, because it cost a protocol feature: this docstring used
    to claim that omitting the ``structured_output`` flag made the SDK derive a
    schema for ``CallToolResult`` itself and raise on every real client call.
    That was true on mcp 1.x. On 2.x the flag is inert, and the claim kept
    output schemas switched off across the whole surface. The lesson is in
    the ``mcp-tool-contracts`` skill: pin SDK behaviour with a test or do not
    assert it. ``tests/integration/test_mcp_surface.py::TestWireLevelConversion``
    now runs every tool through this path, which is the only place the
    difference is visible — direct ``tool.fn`` tests cannot see it.
    """
    json_text = payload.model_dump_json(exclude_none=True)
    return CallToolResult(
        content=[TextContent(type="text", text=json_text)],
        structured_content=payload.model_dump(exclude_none=True),
        is_error=is_error,
    )


def make_success(
    data: JsonMapping | None = None,
    *,
    project: str | None = None,
) -> CallToolResult:
    """Build a successful tool result."""
    return make_result(
        FerumindToolEnvelope(ok=True, data=_to_json_object(data), project=project),
        is_error=False,
    )


def make_rich_success(
    data: JsonMapping | None,
    content: Sequence[ContentBlock],
    *,
    project: str | None = None,
) -> CallToolResult:
    """Build a success result whose content is typed MCP blocks.

    Most Ferumind tools serialize their envelope into a single ``TextContent``
    (see :func:`make_result`). ``read_file`` cannot: an image has to reach
    the host as a genuine ``ImageContent`` block, and a resource has to reach
    it as a genuine ``ResourceLink``, or the host will render base64 as text
    instead of attaching a picture.

    The Ferumind envelope still travels in ``structured_content``, so
    machine-readable fields and error codes keep working unchanged. Binary
    payloads belong **only** in their typed content block — never in the
    envelope, the text blocks, or ``_meta``.
    """
    payload = FerumindToolEnvelope(ok=True, data=_to_json_object(data), project=project)
    return CallToolResult(
        content=list(content),
        structured_content=payload.model_dump(exclude_none=True),
        is_error=False,
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
        FerumindToolEnvelope(
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


def write_result_data(result: WriteResult) -> JsonObject:
    """Project a core :class:`WriteResult` onto the fields tools advertise.

    Shared rather than per-registrar: ``apply_patch`` (``propose_tools``),
    ``create_document``/``capture_note`` (``document_tools``) and
    ``restore_snapshot`` (``lifecycle_tools``) all publish exactly these six,
    and the drops are
    deliberate — ``diff`` is added only where a tool advertises it, and the
    restore/rollback snapshot ids only where they can be non-null.
    """
    return {
        "operation_id": result.operation_id,
        "snapshot_id": result.snapshot_id,
        "path": result.path,
        "folder": result.folder,
        "document_sha256": result.document_sha256,
        "index_error": result.index_error,
    }


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
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )


def proposal_annotations() -> ToolAnnotations:
    """Annotations for pending edit proposal tools.

    Proposal tools do not mutate project files. They read a Markdown
    document, compute a guarded diff, and store pending proposal metadata for
    a later explicit apply step.
    """
    return ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )


def write_annotations() -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )


def open_world_write_annotations() -> ToolAnnotations:
    """Annotations for mutations that fetch caller-selected remote resources."""

    return ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )
