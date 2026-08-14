"""Process-wide logging configuration (REL-020).

``FERUMIND_LOG_LEVEL`` was documented in ``.env.example`` and parsed into
:class:`~ferumind.core.config.Config`, but nothing ever configured logging from
it. This module makes the setting real.

Two constraints shape the implementation:

**Output goes to stderr, never stdout.** The MCP server speaks JSON-RPC on
stdout; a single stray log line there corrupts the protocol stream.

**Some loggers stay pinned regardless of the configured level.** Raising
verbosity must not turn on third-party logging that emits credentials or user
content. See :data:`PINNED_LOGGERS` — that mapping is a security control, not
noise reduction, and pins are applied on every call.
"""

from __future__ import annotations

import logging
import sys
from typing import Final

from ferumind.core.config import LogLevel

#: Loggers held at a fixed level whatever ``FERUMIND_LOG_LEVEL`` says.
#:
#: - ``httpx``/``httpcore`` log the full request URL at INFO. ``remote_fetch``
#:   fetches short-lived *signed* download URLs, which are credentials — the
#:   module docstring promises they are never logged, and this is what keeps
#:   that promise once logging is configured at all.
#: - ``httpx2``/``httpcore2`` are the *same* clients under the names mcp 2.x
#:   depends on. Ferumind keeps plain ``httpx`` for ``remote_fetch``, so both
#:   families exist in the environment and both must be pinned; dropping either
#:   set leaves a live URL-logging path.
#: - ``mcp.server.lowlevel.server`` dumps complete inbound messages, including
#:   tool arguments and patch bodies, at DEBUG. (Logger name unchanged in 2.x.)
PINNED_LOGGERS: Final[dict[str, int]] = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "httpx2": logging.WARNING,
    "httpcore2": logging.WARNING,
    "mcp.server.lowlevel.server": logging.INFO,
}

_LOG_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

_configured = False


def _apply_pins() -> None:
    """Clamp every logger in :data:`PINNED_LOGGERS` to at least its floor."""
    for name, floor in PINNED_LOGGERS.items():
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET or logger.level < floor:
            logger.setLevel(floor)


def configure_logging(level: LogLevel, *, force: bool = False) -> None:
    """Configure root logging once per process, writing to stderr.

    Idempotent: later calls are ignored unless ``force`` is set, so a CLI
    command that starts the MCP server does not reconfigure handlers. The
    security pins are re-applied on every call regardless, because a library
    imported later can reset a logger's level.

    :param level: A validated level name from ``Config.log_level``.
    :param force: Replace existing Ferumind handlers. For tests.
    """
    global _configured

    numeric = getattr(logging, level)
    root = logging.getLogger()

    if not _configured or force:
        for handler in [h for h in root.handlers if getattr(h, "_ferumind", False)]:
            root.removeHandler(handler)
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        # Marked so a forced reconfigure replaces only our own handler and
        # leaves any host-installed handler alone.
        handler._ferumind = True  # type: ignore[attr-defined]
        root.addHandler(handler)
        _configured = True

    root.setLevel(numeric)
    _apply_pins()


def reset_logging_for_tests() -> None:
    """Drop Ferumind's handler and the configured flag."""
    global _configured
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_ferumind", False)]:
        root.removeHandler(handler)
    _configured = False
