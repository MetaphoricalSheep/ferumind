# pyright: reportUnusedFunction=false
"""Tests for FERUMIND_LOG_LEVEL wiring (REL-020).

The security-relevant cases are the pins: raising verbosity must not switch on
third-party logging that emits credentials or user content.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from ferumind.core.config import load_config
from ferumind.core.logging_setup import (
    PINNED_LOGGERS,
    configure_logging,
    reset_logging_for_tests,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(autouse=True)
def _clean_logging() -> object:
    reset_logging_for_tests()
    yield
    reset_logging_for_tests()
    logging.getLogger().setLevel(logging.WARNING)


class TestConfiguredLevel:
    def test_level_is_applied_to_root(self) -> None:
        configure_logging("DEBUG", force=True)
        assert logging.getLogger().level == logging.DEBUG

    def test_level_change_takes_effect(self) -> None:
        configure_logging("ERROR", force=True)
        assert logging.getLogger().level == logging.ERROR
        configure_logging("WARNING")
        assert logging.getLogger().level == logging.WARNING

    def test_log_output_goes_to_stderr_never_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A log line on stdout corrupts the MCP JSON-RPC stream.

        Asserted behaviourally rather than by inspecting ``handler.stream``:
        what matters is where a record lands, not how the handler is built.
        """
        configure_logging("INFO", force=True)
        logging.getLogger("ferumind.test").warning("canary-log-line")
        captured = capsys.readouterr()
        assert "canary-log-line" in captured.err
        assert "canary-log-line" not in captured.out

    def test_configures_once_unless_forced(self) -> None:
        configure_logging("INFO", force=True)
        configure_logging("INFO")
        configure_logging("INFO")
        ours = [h for h in logging.getLogger().handlers if getattr(h, "_ferumind", False)]
        assert len(ours) == 1


class TestSecurityPins:
    """Pinned loggers stay clamped however loud the configured level is."""

    def test_pins_hold_at_debug(self) -> None:
        configure_logging("DEBUG", force=True)
        for name, floor in PINNED_LOGGERS.items():
            assert logging.getLogger(name).level >= floor, name

    def test_httpx_cannot_log_request_urls(self) -> None:
        """httpx logs the full request URL at INFO; remote_fetch handles signed
        download URLs, which are credentials.
        """
        configure_logging("DEBUG", force=True)
        assert logging.getLogger("httpx").level >= logging.WARNING
        assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)

    def test_both_httpx_generations_are_pinned(self) -> None:
        """mcp 2.x depends on ``httpx2``; Ferumind keeps ``httpx`` for
        ``remote_fetch``. Both families are installed, and both log the full
        request URL at INFO, so pinning only one leaves a live leak path.
        """
        configure_logging("DEBUG", force=True)
        for name in ("httpx", "httpcore", "httpx2", "httpcore2"):
            assert name in PINNED_LOGGERS, f"{name} is not pinned"
            assert not logging.getLogger(name).isEnabledFor(logging.INFO), name

    def test_both_httpx_generations_are_actually_installed(self) -> None:
        """A pin on a package that is not present proves nothing.

        If either distribution disappears, this fails so the pin list is
        revisited rather than quietly guarding a logger nobody emits to.
        """
        from importlib.metadata import version as pkg_version

        assert pkg_version("httpx")
        assert pkg_version("httpx2")

    def test_mcp_sdk_cannot_dump_tool_arguments(self) -> None:
        configure_logging("DEBUG", force=True)
        assert not logging.getLogger("mcp.server.lowlevel.server").isEnabledFor(logging.DEBUG)

    def test_pins_reapplied_after_a_library_lowers_them(self) -> None:
        configure_logging("DEBUG", force=True)
        logging.getLogger("httpx").setLevel(logging.DEBUG)  # simulate a library reset
        configure_logging("DEBUG")
        assert logging.getLogger("httpx").level >= logging.WARNING

    def test_signed_url_canary_never_reaches_the_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The concrete leak: an httpx-style INFO line carrying a signed URL."""
        configure_logging("DEBUG", force=True)
        signed = "https://files.example.com/f/abc?sig=SUPERSECRETTOKEN&exp=999"
        with caplog.at_level(logging.DEBUG):
            logging.getLogger("httpx").info('HTTP Request: GET %s "200 OK"', signed)
        assert "SUPERSECRETTOKEN" not in caplog.text
        assert signed not in caplog.text


class TestInvalidValuesFailBeforeServing:
    def test_invalid_level_rejected_by_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FERUMIND_LOG_LEVEL", "LOUD")
        with pytest.raises(ValueError, match="log_level"):
            load_config()

    def test_lowercase_is_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FERUMIND_LOG_LEVEL", "debug")
        assert load_config().log_level == "DEBUG"

    def test_default_is_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FERUMIND_LOG_LEVEL", raising=False)
        assert load_config().log_level == "INFO"


class TestCliSubprocess:
    """End-to-end through the real entry point, not an in-process shim."""

    def _run(
        self, level: str | None, workspace: Path, *args: str
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(Path.home()),
            "FERUMIND_WORKSPACE": str(workspace),
        }
        if level is not None:
            env["FERUMIND_LOG_LEVEL"] = level
        return subprocess.run(
            # Invoke the app object directly: ferumind.cli.main has no
            # __main__ guard, so `python -m` would import and exit 0 without
            # running anything, making every assertion here vacuous.
            [sys.executable, "-c", "from ferumind.cli.main import app; app()", *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
            timeout=120,
        )

    def test_valid_level_runs_a_real_command(self, tmp_path: Path) -> None:
        result = self._run("DEBUG", tmp_path, "info")
        assert result.returncode == 0, result.stderr

    def test_invalid_level_fails_before_the_command_does_anything(self, tmp_path: Path) -> None:
        """``--help`` deliberately still works: Click exits before the callback,
        so a bad env var cannot stop a user reading the help text. Any command
        that actually runs must fail closed.
        """
        result = self._run("LOUD", tmp_path, "info")
        assert result.returncode != 0
        assert "log_level" in (result.stderr + result.stdout)

    def test_help_is_not_broken_by_an_invalid_level(self, tmp_path: Path) -> None:
        result = self._run("LOUD", tmp_path, "--help")
        assert result.returncode == 0

    def test_no_log_output_contaminates_stdout(self, tmp_path: Path) -> None:
        """stdout must carry only command output — the MCP stream lives there."""
        result = self._run("DEBUG", tmp_path, "info")
        assert result.returncode == 0
        for marker in ("DEBUG", "INFO", "WARNING", "CRITICAL"):
            assert f"{marker}    " not in result.stdout
