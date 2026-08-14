"""Drive the Ferumind MCP server over a real stdio pipe.

This is the only part of the harness that knows about processes, framing, or
JSON-RPC. Domain tests speak :class:`SmokeSession` and nothing lower.

**Invocation.** The server is started through ``scripts/ferumind-mcp-stdio``
with an explicit ``--workspace`` flag, and never through the environment. That
combination is the only safe one; ``guard`` explains why at length.

**stdout purity.** MCP stdio has exactly one framing rule: stdout carries
newline-delimited JSON-RPC and nothing else. A stray ``print`` or a logging
handler defaulting to stdout corrupts the stream for every client, and does it
silently — the client sees a parse error, not a traceback. That rule is not
checked here by a separate assertion that someone could forget to call: every
line this session reads from stdout is parsed as JSON-RPC, so contamination
fails the run at the point it happens, with the offending line quoted.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO, Self, cast

from tests.smoke.guard import REPO_ROOT, assert_disposable_workspace, assert_isolated

LAUNCHER = REPO_ROOT / "scripts" / "ferumind-mcp-stdio"

#: MCP protocol revision this harness negotiates.
PROTOCOL_VERSION = "2025-06-18"

#: Seconds to wait for any single reply. Generous: the first call pays for
#: interpreter start, uv resolution, and schema construction.
REPLY_TIMEOUT = 90.0


class SmokeProtocolError(RuntimeError):
    """The server said something that is not a well-formed JSON-RPC reply."""


def _as_mapping(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"expected {what} to be a JSON object, got {type(value).__name__}: {value!r}"
        raise SmokeProtocolError(msg)
    return cast(dict[str, object], value)


@dataclass(frozen=True)
class Envelope:
    """A Ferumind tool result: ``ok`` true means read ``data``, false ``error_code``."""

    ok: bool
    data: dict[str, object]
    error_code: str | None
    message: str | None

    def require_ok(self) -> dict[str, object]:
        """Return ``data``, failing loudly with the server's own error text."""
        if not self.ok:
            msg = f"expected success, got {self.error_code}: {self.message}"
            raise AssertionError(msg)
        return self.data

    def require_error(self, expected_code: str) -> dict[str, object]:
        """Return ``data`` after asserting the machine-readable failure code."""
        if self.ok:
            msg = f"expected error {expected_code}, got a success: {self.data}"
            raise AssertionError(msg)
        if self.error_code != expected_code:
            msg = f"expected error {expected_code}, got {self.error_code}: {self.message}"
            raise AssertionError(msg)
        return self.data

    def string(self, key: str) -> str:
        """Read a required string field out of ``data``."""
        value = self.require_ok().get(key)
        if not isinstance(value, str):
            msg = f"expected data[{key!r}] to be a string, got {value!r}"
            raise AssertionError(msg)
        return value


def _envelope_from(structured: dict[str, object]) -> Envelope:
    ok = structured.get("ok")
    if not isinstance(ok, bool):
        msg = f"tool result has no boolean 'ok' field: {structured!r}"
        raise SmokeProtocolError(msg)
    raw_data = structured.get("data")
    error_code = structured.get("error_code")
    message = structured.get("message")
    return Envelope(
        ok=ok,
        data={} if raw_data is None else _as_mapping(raw_data, "tool data"),
        error_code=error_code if isinstance(error_code, str) else None,
        message=message if isinstance(message, str) else None,
    )


class SmokeSession:
    """One live server subprocess, spoken to over its real stdin/stdout."""

    def __init__(self, workspace: Path) -> None:
        assert_disposable_workspace(workspace)
        self._workspace = workspace.resolve()
        self._next_id = 0
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: list[str] = []

    # ── lifecycle ──────────────────────────────────────────────────────────

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def start(self) -> None:
        """Spawn the launcher and complete the MCP handshake."""
        env = dict(os.environ)
        # Not a safety measure — the guard is. Removing it only keeps the
        # variable from looking meaningful to a reader; .env overwrites it
        # regardless, which is the whole trap.
        env.pop("FERUMIND_WORKSPACE", None)
        # Prove stdout stays clean at the noisiest logging level, matching
        # what REL-028 established for the handshake.
        env["FERUMIND_LOG_LEVEL"] = "DEBUG"
        self._process = subprocess.Popen(
            [str(LAUNCHER), "--workspace", str(self._workspace)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(REPO_ROOT),
            env=env,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self._handshake()

    def close(self) -> None:
        """Close stdin and wait for the server to exit on its own."""
        process = self._process
        if process is None:
            return
        self._process = None
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    # ── plumbing ───────────────────────────────────────────────────────────

    def _pump_stdout(self) -> None:
        process = self._process
        stream = process.stdout if process is not None else None
        if stream is not None:
            for line in stream:
                self._lines.put(line)
        self._lines.put(None)

    def _pump_stderr(self) -> None:
        process = self._process
        stream = process.stderr if process is not None else None
        if stream is not None:
            for line in stream:
                self._stderr.append(line)

    @property
    def stderr_text(self) -> str:
        """Everything the server has written to stderr so far."""
        return "".join(self._stderr)

    def _stdin(self) -> IO[str]:
        process = self._process
        if process is None or process.stdin is None:
            msg = "the smoke session is not running"
            raise SmokeProtocolError(msg)
        return process.stdin

    def _send(self, payload: dict[str, object]) -> None:
        stream = self._stdin()
        stream.write(json.dumps(payload) + "\n")
        stream.flush()

    def _read_reply(self, request_id: int) -> dict[str, object]:
        """Read stdout until the reply to *request_id* arrives.

        Every line is parsed as JSON-RPC. This is where a stray ``print``
        anywhere in the server surfaces.
        """
        while True:
            try:
                line = self._lines.get(timeout=REPLY_TIMEOUT)
            except queue.Empty:
                msg = (
                    f"no reply to request {request_id} within {REPLY_TIMEOUT}s.\n{self._context()}"
                )
                raise SmokeProtocolError(msg) from None
            if line is None:
                msg = f"server exited before replying to request {request_id}.\n{self._context()}"
                raise SmokeProtocolError(msg)
            if not line.strip():
                continue
            message = self._parse_stdout_line(line)
            if message.get("id") == request_id:
                return message

    def _parse_stdout_line(self, line: str) -> dict[str, object]:
        try:
            parsed: object = json.loads(line)
        except json.JSONDecodeError as exc:
            msg = (
                "non-protocol output on stdout — this corrupts the stream for every "
                f"MCP client. Offending line: {line!r}\n{self._context()}"
            )
            raise SmokeProtocolError(msg) from exc
        message = _as_mapping(parsed, "a stdout line")
        if message.get("jsonrpc") != "2.0":
            msg = f"stdout line is JSON but not JSON-RPC 2.0: {line!r}"
            raise SmokeProtocolError(msg)
        return message

    def _context(self) -> str:
        return f"server stderr:\n{self.stderr_text}"

    def _request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        reply = self._read_reply(request_id)
        if "error" in reply:
            msg = f"{method} failed at the protocol level: {reply['error']!r}"
            raise SmokeProtocolError(msg)
        return _as_mapping(reply.get("result"), f"the result of {method}")

    def _handshake(self) -> None:
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ferumind-smoke", "version": "1"},
            },
        )
        server_info = _as_mapping(result.get("serverInfo"), "serverInfo")
        if server_info.get("name") != "Ferumind":
            msg = f"handshake answered by something other than Ferumind: {server_info!r}"
            raise SmokeProtocolError(msg)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    # ── surface ────────────────────────────────────────────────────────────

    def tool_names(self) -> frozenset[str]:
        """Every tool name the server advertises, over the wire."""
        result = self._request("tools/list", {})
        raw = result.get("tools")
        if not isinstance(raw, list):
            msg = f"tools/list did not return a list: {raw!r}"
            raise SmokeProtocolError(msg)
        tools = cast(list[object], raw)
        return frozenset(
            name
            for tool in tools
            if isinstance(name := _as_mapping(tool, "a tool descriptor").get("name"), str)
        )

    def call(self, tool: str, arguments: dict[str, object]) -> Envelope:
        """Call *tool* and return its Ferumind envelope."""
        result = self._request("tools/call", {"name": tool, "arguments": arguments})
        structured = result.get("structuredContent")
        if structured is None:
            msg = f"{tool} returned no structuredContent; every tool must carry the envelope"
            raise SmokeProtocolError(msg)
        return _envelope_from(_as_mapping(structured, f"the envelope of {tool}"))

    def visible_projects(self) -> frozenset[str]:
        """Project keys the running server can see, straight from ``list_projects``."""
        data = self.call("list_projects", {}).require_ok()
        raw = data.get("projects")
        if not isinstance(raw, list):
            msg = f"list_projects did not return a list: {raw!r}"
            raise SmokeProtocolError(msg)
        entries = cast(list[object], raw)
        return frozenset(
            key
            for entry in entries
            if isinstance(key := _as_mapping(entry, "a project entry").get("key"), str)
        )

    def assert_isolated_from_live_data(self, expected: frozenset[str]) -> None:
        """Fail closed unless the server sees only the projects this run created."""
        assert_isolated(self.visible_projects(), expected)
