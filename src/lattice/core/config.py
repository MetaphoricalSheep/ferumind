"""Lattice runtime configuration.

The Pydantic ``Config`` model is the source of truth for configuration
structure and defaults. ``.env`` / ``.env.example`` document which values are
user-configurable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from lattice.core.images import (
    MAX_STORAGE_EDGE,
    MAX_STORAGE_QUALITY,
    MIN_STORAGE_EDGE,
    MIN_STORAGE_QUALITY,
    ImagePolicy,
)

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Config(BaseModel):
    """Lattice runtime configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_path: Path = Field(default=Path("./workspace"))
    lattice_dir_name: Literal[".lattice"] = ".lattice"
    log_level: LogLevel = "INFO"

    #: Storage-time image normalization. Uploaded rasters are downscaled to
    #: ``image_max_edge`` and re-encoded at ``image_jpeg_quality`` before they
    #: are written, so the workspace never accumulates camera-sized originals.
    #: Changing these values and re-running ``lattice compress-images`` is the
    #: supported way to retune the whole workspace.
    image_compression_enabled: bool = True
    image_max_edge: int = Field(default=2560, ge=MIN_STORAGE_EDGE, le=MAX_STORAGE_EDGE)
    image_jpeg_quality: int = Field(default=85, ge=MIN_STORAGE_QUALITY, le=MAX_STORAGE_QUALITY)

    #: Largest response body a caller's transport will carry. The OpenAI
    #: tunnel control plane rejects anything above 10 MiB with HTTP 413, and
    #: that rejection kills the stdio child, so ``resources/read`` refuses
    #: oversized originals up front rather than emitting an undeliverable
    #: reply. Blob payloads are base64-encoded, so the usable original size is
    #: three quarters of this value.
    max_resource_response_bytes: int = Field(default=10 * 1024 * 1024, ge=64 * 1024)

    @property
    def image_policy(self) -> ImagePolicy:
        """The storage policy these settings describe, for core write paths."""
        return ImagePolicy(
            max_edge=self.image_max_edge,
            jpeg_quality=self.image_jpeg_quality,
            enabled=self.image_compression_enabled,
        )


def _env_str(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_int(name: str) -> int | None:
    """Parse an integer environment override, rejecting non-numeric values."""
    raw = _env_str(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str) -> bool | None:
    """Parse a boolean environment override from the usual spellings."""
    raw = _env_str(name)
    if raw is None:
        return None
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def load_config(workspace: Path | None = None) -> Config:
    """Build a ``Config``, optionally overriding the workspace path.

    Workspace resolution order: explicit *workspace* argument, then the
    ``LATTICE_WORKSPACE`` environment variable, then the ``./workspace``
    default.
    """
    workspace_env = _env_str("LATTICE_WORKSPACE")
    resolved_workspace = workspace
    if resolved_workspace is None and workspace_env is not None:
        resolved_workspace = Path(workspace_env)

    defaults = Config()
    compression_enabled = _env_bool("LATTICE_IMAGE_COMPRESSION")
    return Config(
        workspace_path=resolved_workspace or Path("./workspace"),
        log_level=cast(LogLevel, _env_str("LATTICE_LOG_LEVEL") or "INFO"),
        image_compression_enabled=(
            defaults.image_compression_enabled
            if compression_enabled is None
            else compression_enabled
        ),
        image_max_edge=_env_int("LATTICE_IMAGE_MAX_EDGE") or defaults.image_max_edge,
        image_jpeg_quality=_env_int("LATTICE_IMAGE_JPEG_QUALITY") or defaults.image_jpeg_quality,
        max_resource_response_bytes=(
            _env_int("LATTICE_MAX_RESOURCE_BYTES") or defaults.max_resource_response_bytes
        ),
    )
