"""Fixtures for the real-stdio smoke harness.

The session is **session-scoped on purpose**. Starting the server costs an
interpreter launch, a ``uv`` environment resolution, and construction of the
whole tool surface; a tool call after that costs milliseconds. A fixture per
test would make the harness cost proportional to how thoroughly it checks,
which is exactly the pressure that stops people adding checks. One process,
many calls, so the walk grows almost for free.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.smoke.session import SmokeSession

# Projects the harness creates. The isolation guard runs before any of them
# exist, so at handshake time the server must see nothing at all.
SMOKE_PROJECT = "smoke"


@pytest.fixture(scope="session")
def smoke_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A bootstrapped throwaway workspace, outside the checkout.

    ``tmp_path_factory`` puts this under the system temp root, which is what
    lets it clear ``assert_disposable_workspace``. A workspace inside the
    repository is refused no matter how it was created.
    """
    import sys

    repo_root = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(repo_root / "scripts"))
    from bootstrap_workspace import bootstrap

    workspace = tmp_path_factory.mktemp("smoke-workspace") / "workspace"
    bootstrap(workspace, force=False)
    return workspace


@pytest.fixture(scope="session")
def session(smoke_workspace: Path) -> Iterator[SmokeSession]:
    """A live server subprocess, handshaken and proven isolated from live data."""
    with SmokeSession(smoke_workspace) as live:
        # Before a single write: the server must see an empty workspace. If
        # .env redirected it at the owner's live data, their project keys turn
        # up here and the run stops having written nothing.
        live.assert_isolated_from_live_data(frozenset())
        yield live
