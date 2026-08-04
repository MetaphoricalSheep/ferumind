"""Resource-bounded YAML loading for Lattice-controlled data shapes."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Final, cast

import yaml
from yaml.loader import SafeLoader
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from yaml.tokens import (
    AliasToken,
    AnchorToken,
    BlockEndToken,
    BlockMappingStartToken,
    BlockSequenceStartToken,
    FlowMappingEndToken,
    FlowMappingStartToken,
    FlowSequenceEndToken,
    FlowSequenceStartToken,
    Token,
)

DEFAULT_MAX_YAML_BYTES: Final = 256 * 1024
DEFAULT_MAX_YAML_TOKENS: Final = 20_000
DEFAULT_MAX_YAML_DEPTH: Final = 64
_YAML_STRING_TAG: Final = "tag:yaml.org,2002:str"


class YamlSafetyError(yaml.YAMLError):
    """Raised when YAML exceeds Lattice's safe structural subset."""


def safe_load_yaml(
    text: str,
    *,
    max_bytes: int = DEFAULT_MAX_YAML_BYTES,
    max_tokens: int = DEFAULT_MAX_YAML_TOKENS,
    max_depth: int = DEFAULT_MAX_YAML_DEPTH,
) -> object:
    """Load YAML after bounded structural validation.

    Lattice YAML does not need anchors, aliases, or non-string mapping keys.
    Refusing them prevents cyclic/expansive object graphs and cross-parser
    ambiguity. Duplicate keys fail closed instead of silently taking the
    last value.
    """
    if len(text.encode("utf-8")) > max_bytes:
        raise YamlSafetyError(f"YAML exceeds the {max_bytes}-byte limit")

    depth = 0
    token_count = 0
    starts = (
        BlockMappingStartToken,
        BlockSequenceStartToken,
        FlowMappingStartToken,
        FlowSequenceStartToken,
    )
    ends = (BlockEndToken, FlowMappingEndToken, FlowSequenceEndToken)
    # PyYAML ships incomplete function annotations; these casts narrow its two
    # parser entrypoints to the concrete forms used by this bounded loader.
    scan_yaml = cast(Callable[[str], Iterable[Token]], vars(yaml)["scan"])
    compose_yaml = cast(Callable[[str, type[SafeLoader]], Node | None], vars(yaml)["compose"])
    try:
        for token in scan_yaml(text):
            token_count += 1
            if token_count > max_tokens:
                raise YamlSafetyError(f"YAML exceeds the {max_tokens}-token limit")
            if isinstance(token, AliasToken | AnchorToken):
                raise YamlSafetyError("YAML anchors and aliases are not allowed")
            if isinstance(token, starts):
                depth += 1
                if depth > max_depth:
                    raise YamlSafetyError(f"YAML exceeds the maximum depth of {max_depth}")
            elif isinstance(token, ends):
                depth = max(0, depth - 1)

        root = compose_yaml(text, SafeLoader)
        if root is not None:
            _validate_node(root)
        return yaml.safe_load(text)
    except RecursionError as exc:
        raise YamlSafetyError("YAML nesting exceeds the parser safety limit") from exc


def _validate_node(node: Node) -> None:
    if isinstance(node, MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode) or key_node.tag != _YAML_STRING_TAG:
                raise YamlSafetyError("YAML mapping keys must be strings")
            if key_node.value in seen:
                raise YamlSafetyError(f"Duplicate YAML mapping key: {key_node.value!r}")
            seen.add(key_node.value)
            _validate_node(value_node)
        return
    if isinstance(node, SequenceNode):
        for child in node.value:
            _validate_node(child)
