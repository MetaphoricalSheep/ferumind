"""Tests for tunnel launcher scripts."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _script_path(name: str) -> Path:
    return REPO_ROOT / "scripts" / name


def _is_executable(path: Path) -> bool:
    mode = os.stat(str(path)).st_mode
    return bool(mode & stat.S_IXUSR)


def _isolated_tunnel_script(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    tunnel = scripts / "tunnel.sh"
    wrapper = scripts / "ferumind-mcp-stdio"
    shutil.copy2(_script_path("tunnel.sh"), tunnel)
    shutil.copy2(_script_path("ferumind-mcp-stdio"), wrapper)
    environment = dict(os.environ)
    for name in (
        "CI",
        "FERUMIND_TUNNEL_PROFILE",
        "FERUMIND_WORKSPACE",
        "TUNNEL_CLIENT_BIN",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "CONTROL_PLANE_API_KEY": "synthetic-control-plane-key",
            "CONTROL_PLANE_TUNNEL_ID": "synthetic-tunnel-id",
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
        }
    )
    return tunnel, environment


# ── existence ─────────────────────────────────────────────────────────────


def test_tunnel_script_exists() -> None:
    assert _script_path("tunnel.sh").is_file()


def test_tunnel_script_executable() -> None:
    assert _is_executable(_script_path("tunnel.sh"))


def test_mcp_stdio_wrapper_exists() -> None:
    assert _script_path("ferumind-mcp-stdio").is_file()


def test_mcp_stdio_wrapper_executable() -> None:
    assert _is_executable(_script_path("ferumind-mcp-stdio"))


# ── bash syntax ───────────────────────────────────────────────────────────


def test_tunnel_script_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(_script_path("tunnel.sh"))],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Syntax error:\n{result.stderr}"


def test_mcp_stdio_wrapper_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(_script_path("ferumind-mcp-stdio"))],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Syntax error:\n{result.stderr}"


# ── ferumind-mcp-stdio content constraints ─────────────────────────────────


def test_mcp_stdio_wrapper_no_echo() -> None:
    text = _script_path("ferumind-mcp-stdio").read_text()
    assert "echo " not in text, "ferumind-mcp-stdio must not use echo"


def test_mcp_stdio_wrapper_no_printf() -> None:
    text = _script_path("ferumind-mcp-stdio").read_text()
    assert "printf " not in text, "ferumind-mcp-stdio must not use printf"


def test_mcp_stdio_wrapper_execs_uv_run() -> None:
    text = _script_path("ferumind-mcp-stdio").read_text()
    assert "exec uv run ferumind mcp serve" in text


# ── tunnel.sh content constraints ─────────────────────────────────────────


def test_tunnel_script_uses_sample_mcp_stdio_local() -> None:
    text = _script_path("tunnel.sh").read_text()
    assert "sample_mcp_stdio_local" in text


def test_tunnel_script_uses_mcp_command_flag() -> None:
    text = _script_path("tunnel.sh").read_text()
    assert '--mcp-command "$MCP_COMMAND"' in text


def test_tunnel_script_defaults_to_path_tunnel_client() -> None:
    text = _script_path("tunnel.sh").read_text()
    assert 'TUNNEL_CLIENT="${TUNNEL_CLIENT_BIN:-tunnel-client}"' in text


def test_tunnel_script_checks_tunnel_client_on_path() -> None:
    text = _script_path("tunnel.sh").read_text()
    assert 'command -v "$TUNNEL_CLIENT"' in text


def test_tunnel_script_does_not_print_api_key() -> None:
    text = _script_path("tunnel.sh").read_text()
    assert "CONTROL_PLANE_API_KEY" not in text.replace("require_env CONTROL_PLANE_API_KEY", ""), (
        "tunnel.sh must not print CONTROL_PLANE_API_KEY"
    )


def test_tunnel_pid_record_binds_pid_to_process_start_time() -> None:
    text = _script_path("tunnel.sh").read_text()
    assert "/proc/${pid}/stat" in text
    assert 'remainder="${stat_line##*) }"' in text
    assert "awk '{print $22}'" not in text
    assert "actual_start_time" in text
    assert 'is_expected_tunnel_process "$pid" "$start_time"' in text


def test_tunnel_start_refuses_ci() -> None:
    text = _script_path("tunnel.sh").read_text()
    assert 'if [[ -n "${CI:-}" ]]' in text
    assert "Refusing to start a tunnel from CI." in text


def test_tunnel_script_rejects_unknown_arguments(tmp_path: Path) -> None:
    tunnel, environment = _isolated_tunnel_script(tmp_path)

    result = subprocess.run(
        [str(tunnel), "--not-a-real-mode"],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Usage:" in result.stderr


def test_tunnel_script_rejects_multiple_modes(tmp_path: Path) -> None:
    tunnel, environment = _isolated_tunnel_script(tmp_path)

    result = subprocess.run(
        [str(tunnel), "--bg", "--doctor"],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Only one tunnel mode may be selected." in result.stderr


def test_background_start_is_verified_before_pid_record_is_published() -> None:
    text = _script_path("tunnel.sh").read_text()
    identity_check = 'is_expected_tunnel_process "$pid" "$start_time"'
    pid_write = 'printf \'%s %s\\n\' "$pid" "$start_time" >"$pid_file_temp"'
    assert text.index(identity_check, text.index("start_bg()")) < text.index(pid_write)


def test_mcp_wrapper_strips_control_plane_credentials() -> None:
    text = _script_path("ferumind-mcp-stdio").read_text()
    assert "unset CONTROL_PLANE_API_KEY CONTROL_PLANE_TUNNEL_ID" in text


def test_tunnel_start_is_refused_for_any_ci_value(tmp_path: Path) -> None:
    tunnel, environment = _isolated_tunnel_script(tmp_path)
    environment["CI"] = "1"

    result = subprocess.run(
        [str(tunnel), "--bg"],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Refusing to start a tunnel from CI." in result.stderr


def test_background_start_does_not_publish_pid_for_early_exit(tmp_path: Path) -> None:
    tunnel, environment = _isolated_tunnel_script(tmp_path)
    fake_client = tmp_path / "fake-tunnel-client"
    fake_client.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "run" ]]; then\n'
        "  sleep 0.05\n"
        "  exit 23\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_client.chmod(0o700)
    environment["TUNNEL_CLIENT_BIN"] = str(fake_client)

    result = subprocess.run(
        [str(tunnel), "--bg"],
        env=environment,
        capture_output=True,
        text=True,
    )

    pid_file = tmp_path / "config" / "tunnel-client" / "ferumind.pid"
    assert result.returncode != 0
    assert not pid_file.exists()
    assert "exited" in result.stderr


# ── .env.example ──────────────────────────────────────────────────────────


def test_env_example_has_placeholder_values() -> None:
    env_example = REPO_ROOT / ".env.example"
    text = env_example.read_text()
    assert "sk-proj-xxx" in text, ".env.example should contain placeholder API key"
    assert "tunnel_xxx" in text, ".env.example should contain placeholder tunnel ID"
