"""Refuse to smoke-test against the owner's live workspace.

``scripts/ferumind-mcp-stdio`` does ``set -a; source .env``, and ``.env`` sets
``FERUMIND_WORKSPACE=workspace``. Sourcing **overwrites** a variable that was
already exported, so a harness that does ``env FERUMIND_WORKSPACE=/tmp/throwaway
scripts/ferumind-mcp-stdio`` is silently redirected at the checkout's live
``workspace/`` — real personal documents — and then writes documents, uploads,
archives and projects into it. Every assertion still passes. Nothing warns.

Verified mechanically before this module was written::

    $ FERUMIND_WORKSPACE=/tmp/probe bash -c 'set -a; source .env; set +a; echo $FERUMIND_WORKSPACE'
    workspace

Only the explicit ``--workspace`` flag survives, because the launcher ends in
``exec uv run ferumind mcp serve "$@"`` and the flag beats the environment.

So the harness passes ``--workspace`` and never the environment, and this module
makes that a guard rather than a convention. Two layers, both failing closed:

``assert_disposable_workspace``
    Static, before the process is spawned. The path must be a real directory
    outside this checkout. That is strictly stronger than "not
    ``<repo>/workspace``": nothing inside the checkout is disposable, and the
    live path is named in ``.env`` by a relative string that only means
    anything relative to the repo root.

``assert_isolated``
    Dynamic, after ``initialize`` and before the first write. Asks the running
    server what projects it can see. The static check constrains what the
    harness *asks* for; only this one observes what the server actually
    *opened*. If any future change to the launcher, the CLI, or precedence
    re-routes the server at the live workspace, the live project keys surface
    here and the run aborts having written nothing.
"""

from __future__ import annotations

from pathlib import Path

from ferumind.core.paths import is_under_root

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class LiveWorkspaceRefusedError(RuntimeError):
    """Raised when the smoke harness would have touched non-disposable data.

    Deliberately not an ``AssertionError``: this is a refusal to act, not a
    failed expectation, and it must survive ``python -O``.
    """


def declared_env_workspace() -> Path | None:
    """The workspace ``.env`` names, resolved the way the launcher resolves it.

    Read from the file rather than from ``os.environ`` on purpose: the trap is
    precisely that the file's value replaces the environment's, so the file is
    the authority on where the server will land.
    """
    env_file = REPO_ROOT / ".env"
    if not env_file.is_file():
        return None
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("FERUMIND_WORKSPACE="):
            continue
        value = line.partition("=")[2].strip().strip("\"'")
        if not value:
            return None
        candidate = Path(value)
        return candidate if candidate.is_absolute() else REPO_ROOT / candidate
    return None


def assert_disposable_workspace(workspace: Path) -> None:
    """Refuse any workspace that is not a throwaway directory outside the checkout.

    Raises :class:`LiveWorkspaceRefusedError` rather than returning a verdict, so a
    caller cannot proceed by ignoring a return value.
    """
    resolved = workspace.resolve()
    if not resolved.is_dir():
        msg = f"smoke workspace {resolved} is not an existing directory"
        raise LiveWorkspaceRefusedError(msg)
    if is_under_root(resolved, REPO_ROOT):
        msg = (
            f"refusing to smoke-test against {resolved}: it is inside the checkout "
            f"({REPO_ROOT}). The smoke harness only ever runs against a throwaway "
            "directory created for the run."
        )
        raise LiveWorkspaceRefusedError(msg)
    declared = declared_env_workspace()
    if declared is not None and resolved == declared.resolve():
        msg = (
            f"refusing to smoke-test against {resolved}: it is the workspace "
            "declared by .env, which is the owner's live data."
        )
        raise LiveWorkspaceRefusedError(msg)


def assert_isolated(visible_projects: frozenset[str], expected: frozenset[str]) -> None:
    """Refuse to continue when the server can see projects the harness did not create.

    The static check cannot see through the launcher. This one can: the project
    keys come from a ``list_projects`` call answered by the running server, so
    they describe the workspace it actually opened.
    """
    unexpected = visible_projects - expected
    if unexpected:
        msg = (
            "refusing to continue: the server can see projects the smoke harness "
            f"did not create ({sorted(unexpected)}). The workspace it opened is not "
            "the throwaway it was pointed at — most likely .env overwrote "
            "FERUMIND_WORKSPACE and redirected it at live data."
        )
        raise LiveWorkspaceRefusedError(msg)
