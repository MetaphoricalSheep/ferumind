"""The invocation boundary every MCP tool call passes through.

One rule: **no failure at this boundary reaches the client as raw exception
text.** Bad arguments and a crashing tool body both leave as a Ferumind error
envelope with a machine-readable code.

Both concerns live here because both happen inside the SDK's single invocation
seam, ``FuncMetadata.call_fn_with_arg_validation`` — the function that actually
calls ``tool.fn``. Nothing outside that seam can substitute:

* Argument validation runs *before* the tool function is entered, so an
  ordinary tool wrapper never sees the failure.
* A raising tool body is caught one frame out by ``Tool.run``, which re-raises
  it as ``ToolError(f"Error executing tool {name}: {e}")``; ``MCPServer``
  then returns that string as tool content. Verified against mcp 2.0.0: a bare
  server answers a ``RuntimeError("SECRET")`` with
  ``"Error executing tool …: SECRET"`` on the wire. Ferumind tools handle their
  own domain errors, so anything reaching here is a bug — and a bug's message
  can carry an absolute path, a signed download URL, or document text.

Observation is deliberately *not* here. It is
:class:`ferumind.mcp.observation.CallObservationMiddleware`, which needs no
access to tools at all. The two layers meet only through the correlation id
that middleware mints, so a user-facing ``INTERNAL_ERROR`` quotes the same id
as its observation row.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver.utilities.func_metadata import FuncMetadata
from mcp.types import CallToolResult
from pydantic_core import ValidationError as PydanticValidationError

from ferumind.mcp.models import make_error, strip_schema_titles
from ferumind.mcp.observation import current_correlation_id
from ferumind.mcp.sdk_internals import registered_tools
from ferumind.mcp.tool_context import record_internal_error

logger = logging.getLogger(__name__)


def _safe_error_log(message: str, *args: object) -> None:
    """Never let a broken operator log handler replace a safe MCP envelope."""

    try:
        logger.error(message, *args)
    except Exception:  # Logging is observability and must not alter tool work.
        return


class _GuardedFuncMetadata(FuncMetadata):
    """Turn every framework-level tool failure into a Ferumind envelope."""

    def pre_parse_json(self, data: dict[str, Any]) -> dict[str, Any]:
        """Keep wire types intact instead of parsing JSON nested inside strings."""

        return data.copy()

    async def call_fn_with_arg_validation(
        self,
        fn: Callable[..., Any | Awaitable[Any]],
        fn_is_async: bool,
        arguments_to_validate: dict[str, Any],
        arguments_to_pass_directly: dict[str, Any] | None,
        pre_validated: dict[str, Any] | None = None,
    ) -> Any:
        try:
            return await super().call_fn_with_arg_validation(
                fn,
                fn_is_async,
                arguments_to_validate,
                arguments_to_pass_directly,
                pre_validated,
            )
        except PydanticValidationError:
            # Pydantic's exception text quotes the rejected input values. Never
            # let it reach the SDK's ToolError rendering or the transport log.
            return make_error(
                "VALIDATION_ERROR",
                "Tool arguments do not match the declared input schema",
            )
        except Exception as exc:
            return _sanitized_internal_error(exc)


def _sanitized_internal_error(exc: Exception) -> CallToolResult:
    """Replace an unhandled tool exception with a correlated, opaque envelope.

    The exception *type* is logged for the operator; the message never is, and
    never leaves the process. The correlation id is the only thing a user can
    quote, and it matches the observation row middleware writes for this call.
    """
    correlation_id = current_correlation_id()
    record_internal_error(exc, correlation_id)
    _safe_error_log(
        "Unhandled exception in an MCP tool (correlation_id=%s, type=%s)",
        correlation_id,
        type(exc).__name__,
    )
    return make_error(
        "INTERNAL_ERROR",
        "Ferumind encountered an unexpected internal error",
        {"correlation_id": correlation_id},
    )


def apply_tool_boundary(mcp: object) -> None:
    """Install strict argument validation and error sanitisation on every tool.

    Must run **after** every ``register_*_tools()`` call. Idempotent: a tool
    whose metadata is already guarded is left alone, so repeated registration
    cannot stack boundaries.

    Fails closed via :func:`ferumind.mcp.sdk_internals.registered_tools` — a
    server that cannot install this boundary is one that would answer bad input
    with pydantic's rendering of that input, and a crash with the crash text.
    """
    guarded = 0
    for tool in registered_tools(mcp):
        metadata = tool.fn_metadata
        if isinstance(metadata, _GuardedFuncMetadata):
            continue
        arg_model = metadata.arg_model
        # Mutate the ``ConfigDict`` in place rather than rebuilding it from a
        # widened ``dict``: the SDK's generated model may carry config keys
        # Ferumind has no opinion on, and round-tripping through ``dict()``
        # both loses their types and risks dropping future ones.
        arg_model.model_config["extra"] = "forbid"
        arg_model.model_config["strict"] = True
        arg_model.model_rebuild(force=True)
        tool.parameters["additionalProperties"] = False
        output_schema = metadata.output_schema
        if output_schema is not None:
            # Mutated in place: ``Tool.output_schema`` is a cached_property over
            # this same dict, so trimming the object is safe whether or not
            # anything has already read it.
            strip_schema_titles(output_schema)
        tool.fn_metadata = _GuardedFuncMetadata(
            arg_model=arg_model,
            output_schema=output_schema,
            output_model=metadata.output_model,
            wrap_output=metadata.wrap_output,
        )
        guarded += 1
    logger.debug("Installed the Ferumind tool boundary on %d tool(s)", guarded)
