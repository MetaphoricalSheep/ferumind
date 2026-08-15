"""The package's own version, read from installed distribution metadata.

One reader, because three surfaces report this number — ``ferumind
--version``, ``ferumind info``, and the MCP ``serverInfo`` block — and a
second implementation is how they start disagreeing.

``pyproject.toml`` is the source of truth; this reads what was installed from
it. In an editable checkout that metadata is refreshed by ``uv sync``, so a
freshly edited version can lag until the environment is re-synced. That is a
property of editable installs, not a fallback: nothing here guesses.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

#: Reported when Ferumind is imported without being installed at all — a bare
#: ``sys.path`` insertion, for instance. It is deliberately not a plausible
#: version: a wrong number is worse than an obviously absent one.
UNKNOWN_VERSION = "0+unknown"


def package_version() -> str:
    """Return the installed Ferumind version, or ``UNKNOWN_VERSION``.

    An identity string, not a compatibility promise. What the number does and
    does not guarantee is documented in ``docs/releases.md``.
    """
    try:
        return version("ferumind")
    except PackageNotFoundError:  # pragma: no cover - only when run un-installed
        return UNKNOWN_VERSION
