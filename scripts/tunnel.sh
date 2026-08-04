#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

load_env_file() {
  local env_file="$1"
  if [[ -f "$env_file" ]]; then
    if [[ -L "$env_file" ]]; then
      echo "Refusing symlinked env file: $env_file" >&2
      exit 1
    fi
    local mode owner current_uid
    mode="$(stat -c '%a' "$env_file")"
    owner="$(stat -c '%u' "$env_file")"
    current_uid="$(id -u)"
    if (( (8#$mode & 077) != 0 )); then
      echo "Refusing insecure env file permissions on $env_file (expected 0600 or stricter)." >&2
      exit 1
    fi
    if [[ "$owner" != "$current_uid" && "$owner" != "0" ]]; then
      echo "Refusing env file not owned by the current user or root: $env_file" >&2
      exit 1
    fi
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  fi
}

load_env_file "${REPO_ROOT}/.env"
load_env_file "/etc/lattice-tunnel.env"
load_env_file "/etc/lattice-mcp.env"

PROFILE="${LATTICE_TUNNEL_PROFILE:-lattice}"
MCP_COMMAND="${SCRIPT_DIR}/lattice-mcp-stdio"
TUNNEL_CLIENT="${TUNNEL_CLIENT_BIN:-tunnel-client}"
PROFILE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/tunnel-client"
PROFILE_FILE="${PROFILE_DIR}/${PROFILE}.yaml"
PID_FILE="${PROFILE_DIR}/${PROFILE}.pid"
LOG_FILE="${PROFILE_DIR}/${PROFILE}.log"

if [[ ! "$PROFILE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  echo "Invalid LATTICE_TUNNEL_PROFILE; use 1-64 letters, digits, '.', '_' or '-'." >&2
  exit 1
fi

if [[ -L "$PROFILE_DIR" ]]; then
  echo "Refusing symlinked tunnel profile directory: $PROFILE_DIR" >&2
  exit 1
fi
mkdir -p "$PROFILE_DIR"
chmod 700 "$PROFILE_DIR"
if [[ "$(stat -c '%u' "$PROFILE_DIR")" != "$(id -u)" ]]; then
  echo "Refusing tunnel profile directory not owned by the current user." >&2
  exit 1
fi
for managed_file in "$PROFILE_FILE" "$PID_FILE" "$LOG_FILE"; do
  if [[ -L "$managed_file" ]]; then
    echo "Refusing symlinked tunnel state file: $managed_file" >&2
    exit 1
  fi
done

usage() {
  cat <<EOF
Usage: $(basename "$0") [--init | --force | --bg | --stop | --doctor | --help]

  no args    Validate config and start the tunnel (foreground).
  --init     Create the tunnel profile once if it does not exist, then start.
  --force    Replace existing tunnel profile, then start.
  --bg       Start the tunnel in the background (detached).
  --stop     Stop the background tunnel.
  --doctor   Validate the profile only.
  --help     Show this help.

The tunnel serves the workspace configured by LATTICE_WORKSPACE. The relay is
the only thing standing between a caller and that workspace; the MCP server
itself does not authenticate.

Profile: ${PROFILE}
Profile file: ${PROFILE_FILE}
MCP command: ${MCP_COMMAND}
EOF
}

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required environment variable: ${name}" >&2
    exit 1
  fi
}

init_profile() {
  local -a force_args=()
  if [[ "${1:-}" == "--force" ]]; then
    force_args=(--force)
  fi

  "$TUNNEL_CLIENT" init \
    "${force_args[@]}" \
    --sample sample_mcp_stdio_local \
    --profile "$PROFILE" \
    --tunnel-id "$CONTROL_PLANE_TUNNEL_ID" \
    --mcp-command "$MCP_COMMAND"
}

doctor_profile() {
  "$TUNNEL_CLIENT" doctor --profile "$PROFILE" --explain
}

run_profile() {
  "$TUNNEL_CLIENT" run --profile "$PROFILE" --log.level=info
}

is_running() {
  if [[ ! -f "$PID_FILE" || -L "$PID_FILE" ]]; then
    return 1
  fi
  local record pid start_time
  record="$(read_valid_pid_record 2>/dev/null)" || return 1
  read -r pid start_time <<<"$record"
  kill -0 "$pid" 2>/dev/null &&
    is_expected_tunnel_process "$pid" "$start_time"
}

is_expected_tunnel_process() {
  local pid="$1"
  local expected_start_time="$2"
  local cmdline_file="/proc/${pid}/cmdline"
  local stat_file="/proc/${pid}/stat"
  if [[ ! -r "$cmdline_file" || ! -r "$stat_file" ]]; then
    return 1
  fi
  local cmdline actual_start_time
  actual_start_time="$(read_process_start_time "$pid")" || return 1
  if [[ ! "$actual_start_time" =~ ^[0-9]+$ || "$actual_start_time" != "$expected_start_time" ]]; then
    return 1
  fi
  cmdline="$(tr '\0' ' ' <"$cmdline_file")"
  local client_name="${TUNNEL_CLIENT##*/}"
  [[ "$cmdline" == *"$client_name"* && "$cmdline" == *"--profile $PROFILE"* ]]
}

read_process_start_time() {
  local pid="$1"
  local stat_line remainder start_time
  local -a stat_fields
  IFS= read -r stat_line <"/proc/${pid}/stat" || return 1
  # Field 2 (comm) is parenthesized and may itself contain spaces or ')'.
  # Strip through the final ') ' first; starttime (field 22) is then word 20.
  remainder="${stat_line##*) }"
  if [[ "$remainder" == "$stat_line" ]]; then
    return 1
  fi
  read -r -a stat_fields <<<"$remainder"
  if (( ${#stat_fields[@]} <= 19 )); then
    return 1
  fi
  start_time="${stat_fields[19]}"
  if [[ ! "$start_time" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  printf '%s\n' "$start_time"
}

read_valid_pid_record() {
  if [[ ! -f "$PID_FILE" || -L "$PID_FILE" ]]; then
    echo "PID file is missing, not regular, or is a symlink: $PID_FILE" >&2
    return 1
  fi
  local pid start_time extra
  read -r pid start_time extra <"$PID_FILE"
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || (( pid <= 1 )) ||
    [[ ! "$start_time" =~ ^[0-9]+$ || -n "${extra:-}" ]]; then
    echo "Refusing invalid PID/start-time record in $PID_FILE." >&2
    return 1
  fi
  printf '%s %s\n' "$pid" "$start_time"
}

stop_profile() {
  if ! [[ -f "$PID_FILE" ]]; then
    echo "No PID file found at $PID_FILE — nothing to stop." >&2
    exit 1
  fi

  local record pid start_time
  record="$(read_valid_pid_record)" || exit 1
  read -r pid start_time <<<"$record"

  if ! kill -0 "$pid" 2>/dev/null; then
    echo "Process $pid is not running. Cleaning up stale PID file." >&2
    rm -f "$PID_FILE"
    exit 0
  fi
  if ! is_expected_tunnel_process "$pid" "$start_time"; then
    echo "Refusing to signal PID $pid: it is not the expected tunnel-client profile." >&2
    exit 1
  fi

  echo "Stopping tunnel (PID $pid)..."
  kill "$pid"

  local i
  for i in $(seq 1 10); do
    if ! kill -0 "$pid" 2>/dev/null ||
      ! is_expected_tunnel_process "$pid" "$start_time"; then
      rm -f "$PID_FILE"
      echo "Tunnel stopped."
      return 0
    fi
    sleep 0.5
  done

  echo "Process did not exit after 5s, sending SIGKILL." >&2
  if ! is_expected_tunnel_process "$pid" "$start_time"; then
    echo "Refusing SIGKILL: PID identity changed while waiting." >&2
    exit 1
  fi
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "Tunnel killed."
}

start_bg() {
  if is_running; then
    echo "Tunnel is already running (PID $(cat "$PID_FILE"))." >&2
    echo "Stop it first with: $(basename "$0") --stop" >&2
    exit 1
  fi

  rm -f "$PID_FILE"

  echo "Starting tunnel in background..."
  nohup "$TUNNEL_CLIENT" run --profile "$PROFILE" --log.level=info \
    >"$LOG_FILE" 2>&1 &
  local pid=$!
  local start_time
  start_time="$(read_process_start_time "$pid" 2>/dev/null || true)"
  if [[ ! "$start_time" =~ ^[0-9]+$ ]]; then
    echo "Tunnel process exited before its identity could be recorded." >&2
    wait "$pid" 2>/dev/null || true
    exit 1
  fi
  sleep 0.2
  if ! kill -0 "$pid" 2>/dev/null; then
    wait "$pid" 2>/dev/null || true
    echo "Tunnel process exited during startup. Inspect $LOG_FILE." >&2
    exit 1
  fi
  if ! is_expected_tunnel_process "$pid" "$start_time"; then
    echo "Tunnel process identity changed during startup; no PID file was written." >&2
    exit 1
  fi
  local pid_file_temp="${PID_FILE}.tmp.$$"
  printf '%s %s\n' "$pid" "$start_time" >"$pid_file_temp"
  mv -f -- "$pid_file_temp" "$PID_FILE"
  disown "$pid"

  echo "Tunnel started (PID $pid)."
  echo "Log: $LOG_FILE"
  echo "Stop with: $(basename "$0") --stop"
}

MODE=""
while (( $# > 0 )); do
  argument="$1"
  case "$argument" in
  -h|--help)
    usage
    exit 0
    ;;
  --init|--force|--bg|--stop|--doctor)
    if [[ -n "$MODE" ]]; then
      echo "Only one tunnel mode may be selected." >&2
      usage >&2
      exit 1
    fi
    MODE="$argument"
    shift
    ;;
  *)
    usage >&2
    exit 1
    ;;
  esac
done

if [[ "$MODE" == "--stop" ]]; then
  stop_profile
  exit 0
fi

case "$MODE" in
  ""|--init|--force|--bg)
    if [[ -n "${CI:-}" ]]; then
      echo "Refusing to start a tunnel from CI." >&2
      exit 1
    fi
    echo "Note: serving the configured workspace over the relay." >&2
    ;;
esac

require_env CONTROL_PLANE_TUNNEL_ID
require_env CONTROL_PLANE_API_KEY

if ! command -v "$TUNNEL_CLIENT" >/dev/null 2>&1; then
  echo "tunnel-client command not found: $TUNNEL_CLIENT" >&2
  echo "Install tunnel-client or set TUNNEL_CLIENT_BIN=/path/to/tunnel-client." >&2
  exit 1
fi

if [[ ! -x "$MCP_COMMAND" ]]; then
  echo "MCP command is not executable: $MCP_COMMAND" >&2
  exit 1
fi

case "$MODE" in
  --init)
    if [[ -f "$PROFILE_FILE" ]]; then
      echo "Profile '$PROFILE' already exists at $PROFILE_FILE; skipping init."
      echo "Use --force to replace it."
    else
      echo "Creating tunnel profile: $PROFILE"
      init_profile
    fi
    ;;
  --force)
    echo "Replacing tunnel profile: $PROFILE"
    init_profile --force
    ;;
  --bg)
    echo "Checking tunnel profile: $PROFILE"
    doctor_profile
    start_bg
    exit 0
    ;;
  --doctor)
    echo "Checking tunnel profile: $PROFILE"
    doctor_profile
    exit 0
    ;;
  "")
    ;;
esac

echo "Checking tunnel profile: $PROFILE"
doctor_profile

echo "Starting tunnel: $PROFILE"
run_profile
