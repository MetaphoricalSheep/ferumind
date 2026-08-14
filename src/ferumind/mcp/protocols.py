"""Typing helpers for MCP tool registration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ParamSpec, Protocol, TypeVar

from mcp.types import ToolAnnotations

P = ParamSpec("P")
R = TypeVar("R")


class ToolDecorator(Protocol):
    """Decorator returned by ``mcp.tool`` that preserves function types."""

    def __call__(self, func: Callable[P, R], /) -> Callable[P, R]: ...


class ToolRegistrar(Protocol):
    """Minimal MCP registration surface used by this project.

    ``structured_output`` is optional and Ferumind never passes it. Schema
    derivation is driven entirely by the return annotation
    ``Annotated[CallToolResult, FerumindResult[...]]``; passing ``False`` here
    would short-circuit ``func_metadata`` *before* that annotation is read and
    silently strip every ``outputSchema`` from the surface. It stays in the
    signature only because the SDK accepts it.
    """

    def tool(
        self,
        *,
        name: str,
        title: str,
        description: str,
        annotations: ToolAnnotations,
        structured_output: bool | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ToolDecorator: ...
