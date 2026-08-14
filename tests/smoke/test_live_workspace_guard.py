"""Prove the live-workspace guard fires.

A data-safety guard nobody has watched fail is not a guard. These tests point
the harness at the owner's real workspace and show it refusing, and they encode
the environment trap that makes the guard necessary in the first place, so that
a future change to ``.env`` or to the launcher cannot quietly remove the reason
without failing here.

Nothing in this file starts a server. The refusals all happen before a process
could be spawned, which is the property under test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.smoke.guard import (
    REPO_ROOT,
    LiveWorkspaceRefusedError,
    assert_disposable_workspace,
    assert_isolated,
    declared_env_workspace,
)
from tests.smoke.session import SmokeSession

pytestmark = pytest.mark.smoke

ENV_FILE = REPO_ROOT / ".env"
LIVE_WORKSPACE = REPO_ROOT / "workspace"


class TestTheTrapIsReal:
    """The conditions that make the guard necessary, asserted rather than assumed."""

    @pytest.mark.skipif(not ENV_FILE.is_file(), reason="no .env in this checkout")
    def test_sourcing_dotenv_overwrites_a_preset_workspace_variable(self) -> None:
        """``set -a; source .env`` clobbers the environment the caller set.

        This is the whole trap. ``scripts/ferumind-mcp-stdio`` sources ``.env``
        exactly this way, so exporting ``FERUMIND_WORKSPACE=/tmp/throwaway``
        and running the launcher lands the server on whatever ``.env`` says.
        """
        if declared_env_workspace() is None:
            pytest.skip(".env does not declare FERUMIND_WORKSPACE")

        result = subprocess.run(
            [
                "bash",
                "-c",
                'set -a; source .env >/dev/null 2>&1; set +a; printf "%s" "$FERUMIND_WORKSPACE"',
            ],
            cwd=REPO_ROOT,
            env={"PATH": "/usr/bin:/bin", "FERUMIND_WORKSPACE": "/tmp/smoke-preset-value"},
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout != "/tmp/smoke-preset-value", (
            "the pre-set value survived, so the trap this guard exists for is gone — "
            "confirm that deliberately before deleting the guard"
        )

    @pytest.mark.skipif(not ENV_FILE.is_file(), reason="no .env in this checkout")
    def test_the_workspace_dotenv_names_is_inside_the_checkout(self) -> None:
        declared = declared_env_workspace()
        if declared is None:
            pytest.skip(".env does not declare FERUMIND_WORKSPACE")

        assert declared.resolve().is_relative_to(REPO_ROOT)


class TestTheStaticGuardRefuses:
    """``assert_disposable_workspace`` fails closed, before anything is spawned."""

    @pytest.mark.skipif(not LIVE_WORKSPACE.is_dir(), reason="no live workspace in this checkout")
    def test_the_owners_live_workspace_is_refused(self) -> None:
        with pytest.raises(LiveWorkspaceRefusedError, match="inside the checkout"):
            assert_disposable_workspace(LIVE_WORKSPACE)

    def test_any_directory_inside_the_checkout_is_refused(self) -> None:
        """Not a denylist of one path: nothing in the repository is disposable."""
        with pytest.raises(LiveWorkspaceRefusedError, match="inside the checkout"):
            assert_disposable_workspace(REPO_ROOT / "src")

    def test_a_relative_path_that_resolves_into_the_checkout_is_refused(self) -> None:
        """The guard resolves before judging, so ``./workspace`` cannot sneak through."""
        with pytest.raises(LiveWorkspaceRefusedError):
            assert_disposable_workspace(Path("src"))

    def test_a_nonexistent_directory_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(LiveWorkspaceRefusedError, match="not an existing directory"):
            assert_disposable_workspace(tmp_path / "never-created")

    def test_a_throwaway_directory_outside_the_checkout_is_allowed(self, tmp_path: Path) -> None:
        assert_disposable_workspace(tmp_path)

    def test_the_session_refuses_before_it_spawns_anything(self) -> None:
        """Constructing the session is where the refusal has to happen.

        If the check lived in ``start`` instead, a caller could build a session
        aimed at live data and the guard would depend on call order.
        """
        with pytest.raises(LiveWorkspaceRefusedError):
            SmokeSession(REPO_ROOT / "src")


class TestTheDynamicGuardRefuses:
    """``assert_isolated`` catches a redirect the static check cannot see."""

    def test_projects_the_harness_did_not_create_abort_the_run(self) -> None:
        """What a redirected server looks like: the owner's real project keys."""
        with pytest.raises(LiveWorkspaceRefusedError, match="did not create"):
            assert_isolated(frozenset({"personal", "work"}), frozenset())

    def test_an_empty_workspace_passes(self) -> None:
        assert_isolated(frozenset(), frozenset())

    def test_only_the_projects_the_run_created_pass(self) -> None:
        assert_isolated(frozenset({"smoke"}), frozenset({"smoke"}))
