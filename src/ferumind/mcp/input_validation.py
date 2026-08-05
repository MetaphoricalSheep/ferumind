"""Strict FastMCP input validation with Ferumind-shaped failures.

FastMCP's generated argument models currently accept coercion and ignore
unknown top-level keys. Validation also happens before a registered tool
function is called, so an ordinary tool wrapper cannot turn those failures
into Ferumind's structured error envelope. This module hardens that private
framework boundary in one place.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from pydantic import ConfigDict
from pydantic_core import ValidationError as PydanticValidationError

from ferumind.mcp.models import make_error


class _StrictFuncMetadata(FuncMetadata):
    """Convert framework argument-validation failures into safe tool results."""

    def pre_parse_json(self, data: dict[str, Any]) -> dict[str, Any]:
        """Keep wire types intact instead of parsing JSON nested inside strings."""

        return data.copy()

    async def call_fn_with_arg_validation(
        self,
        fn: Callable[..., Any | Awaitable[Any]],
        fn_is_async: bool,
        arguments_to_validate: dict[str, Any],
        arguments_to_pass_directly: dict[str, Any] | None,
    ) -> Any:
        try:
            return await super().call_fn_with_arg_validation(
                fn,
                fn_is_async,
                arguments_to_validate,
                arguments_to_pass_directly,
            )
        except PydanticValidationError:
            # Pydantic's exception text contains rejected input values. Never
            # let it reach the SDK's ToolError rendering or transport logs.
            return make_error(
                "VALIDATION_ERROR",
                "Tool arguments do not match the declared input schema",
            )


def apply_strict_input_validation(mcp: Any) -> None:
    """Forbid unknown/coerced top-level arguments on every registered tool.

    ``Any`` and private attributes are required at this framework integration
    boundary: FastMCP exposes neither a public tool iterator nor a hook for
    replacing its generated Pydantic argument metadata.
    """

    tool_manager = getattr(mcp, "_tool_manager", None)
    tools: dict[str, Any] | None = getattr(tool_manager, "_tools", None)
    if tools is None:
        raise RuntimeError("FastMCP tool registry is unavailable")

    for tool in tools.values():
        metadata = tool.fn_metadata
        arg_model = metadata.arg_model
        model_config = dict(arg_model.model_config)
        model_config.update(extra="forbid", strict=True)
        arg_model.model_config = ConfigDict(**model_config)
        arg_model.model_rebuild(force=True)
        tool.parameters["additionalProperties"] = False
        tool.fn_metadata = _StrictFuncMetadata(
            arg_model=arg_model,
            output_schema=metadata.output_schema,
            output_model=metadata.output_model,
            wrap_output=metadata.wrap_output,
        )
