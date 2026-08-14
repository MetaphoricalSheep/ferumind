"""Ferumind runtime configuration.

The Pydantic ``Config`` model is the source of truth for configuration
structure and defaults. ``.env`` / ``.env.example`` document which values are
user-configurable.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from ferumind.core.images import (
    MAX_STORAGE_EDGE,
    MAX_STORAGE_QUALITY,
    MIN_STORAGE_EDGE,
    MIN_STORAGE_QUALITY,
    ImagePolicy,
)

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

#: One megabyte, binary. ``FERUMIND_MAX_RESOURCE_MB`` is stated in these
#: because the limit it describes — the OpenAI tunnel control plane's 10 MiB
#: ceiling — is itself binary. Decimal megabytes would put the default
#: 485,760 bytes below that ceiling for no reason.
BYTES_PER_MB: Final = 1024 * 1024


class Config(BaseModel):
    """Ferumind runtime configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_path: Path = Field(default=Path("./workspace"))
    ferumind_dir_name: Literal[".ferumind"] = ".ferumind"
    log_level: LogLevel = "INFO"

    #: Storage-time image normalization. Uploaded rasters are downscaled to
    #: ``image_max_edge`` and re-encoded at ``image_jpeg_quality`` before they
    #: are written, so the workspace never accumulates camera-sized originals.
    #: Changing these values and re-running ``ferumind compress-images`` is the
    #: supported way to retune the whole workspace.
    image_compression_enabled: bool = True
    image_max_edge: int = Field(default=2560, ge=MIN_STORAGE_EDGE, le=MAX_STORAGE_EDGE)
    image_jpeg_quality: int = Field(default=85, ge=MIN_STORAGE_QUALITY, le=MAX_STORAGE_QUALITY)

    #: Largest response body a caller's transport will carry, in bytes. The
    #: OpenAI tunnel control plane rejects anything above 10 MiB with HTTP
    #: 413, and that rejection kills the stdio child, so ``resources/read``
    #: refuses oversized originals up front rather than emitting an
    #: undeliverable reply. Blob payloads are base64-encoded, so the usable
    #: original size is three quarters of this value.
    #:
    #: Configured as ``FERUMIND_MAX_RESOURCE_MB`` because a transport ceiling
    #: is something people reason about in megabytes; the conversion happens
    #: at the environment boundary and everything inside stays in bytes.
    max_resource_response_bytes: int = Field(default=10 * BYTES_PER_MB, ge=64 * 1024)

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


def _env_mb_as_bytes(name: str) -> int | None:
    """Parse a megabyte environment override into bytes.

    Fractional values are accepted so the whole configurable range stays
    reachable: the field floor is 64 KiB, which whole megabytes could not
    express. Zero and negatives are rejected here rather than falling through
    to the default, because a caller who writes ``=0`` means to forbid
    something and should be told that is not a supported way to say it.
    """
    raw = _env_str(name)
    if raw is None:
        return None
    try:
        megabytes = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number of megabytes, got {raw!r}") from exc
    if not math.isfinite(megabytes) or megabytes <= 0:
        raise ValueError(f"{name} must be a positive number of megabytes, got {raw!r}")
    return round(megabytes * BYTES_PER_MB)


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
    ``FERUMIND_WORKSPACE`` environment variable, then the ``./workspace``
    default.
    """
    workspace_env = _env_str("FERUMIND_WORKSPACE")
    resolved_workspace = workspace
    if resolved_workspace is None and workspace_env is not None:
        resolved_workspace = Path(workspace_env)

    defaults = Config()
    compression_enabled = _env_bool("FERUMIND_IMAGE_COMPRESSION")
    return Config(
        workspace_path=resolved_workspace or Path("./workspace"),
        # Case is incidental to a level name, so normalize it; anything that is
        # still not a valid level fails Config validation here, before any
        # command runs or the server starts serving.
        log_level=cast(LogLevel, (_env_str("FERUMIND_LOG_LEVEL") or "INFO").strip().upper()),
        image_compression_enabled=(
            defaults.image_compression_enabled
            if compression_enabled is None
            else compression_enabled
        ),
        image_max_edge=_env_int("FERUMIND_IMAGE_MAX_EDGE") or defaults.image_max_edge,
        image_jpeg_quality=_env_int("FERUMIND_IMAGE_JPEG_QUALITY") or defaults.image_jpeg_quality,
        max_resource_response_bytes=(
            _env_mb_as_bytes("FERUMIND_MAX_RESOURCE_MB") or defaults.max_resource_response_bytes
        ),
    )
