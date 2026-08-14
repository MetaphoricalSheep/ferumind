"""Loopback-only, read-only HTTP server for the operator dashboard."""

from __future__ import annotations

import json
import logging
import re
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Final, cast
from urllib.parse import urlsplit

from ferumind.core.diagnostics import DiagnosticsService
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.types import JsonObject
from ferumind.dashboard.api import DashboardApi, DashboardApiError

logger = logging.getLogger(__name__)

DEFAULT_DASHBOARD_PORT = 8765
DASHBOARD_HOST = "127.0.0.1"
MAX_REQUEST_TARGET_CHARS = 8_192

_LOCAL_HOST = re.compile(r"^(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?$", re.IGNORECASE)
_LOCAL_IPV6_HOST = re.compile(r"^\[::1\](?::[0-9]{1,5})?$")

_STATIC_ASSETS: Final[dict[str, tuple[str, str]]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/static/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    "/static/basecoat/tokens.css": ("basecoat/tokens.css", "text/css; charset=utf-8"),
    "/static/basecoat/base.css": ("basecoat/base.css", "text/css; charset=utf-8"),
    "/static/basecoat/components.css": (
        "basecoat/components.css",
        "text/css; charset=utf-8",
    ),
    "/static/basecoat/REVISION": ("basecoat/REVISION", "text/plain; charset=utf-8"),
}

_SECURITY_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    (
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self'; connect-src 'self'; img-src 'self' data:; "
        "script-src 'self'; style-src 'self'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)


@dataclass(frozen=True)
class _ResponseOptions:
    content_type: str
    head_only: bool = False
    extra_headers: tuple[tuple[str, str], ...] = ()


class DashboardHttpServer(ThreadingHTTPServer):
    """HTTP server carrying only the typed dashboard API dependency."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, api: DashboardApi) -> None:
        self.dashboard_api = api
        super().__init__((DASHBOARD_HOST, port), DashboardRequestHandler)


def _error_body(code: str, message: str) -> JsonObject:
    return {"ok": False, "error_code": code, "message": message}


def _asset_bytes(relative_path: str) -> bytes:
    static_root = resources.files("ferumind.dashboard").joinpath("static")
    asset = static_root
    for component in relative_path.split("/"):
        asset = asset.joinpath(component)
    return asset.read_bytes()


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Serve a fixed asset map and the local metadata-only JSON API."""

    protocol_version = "HTTP/1.1"
    server_version = "Ferumind"
    sys_version = ""

    def version_string(self) -> str:
        return "Ferumind"

    @property
    def dashboard_server(self) -> DashboardHttpServer:
        return cast(DashboardHttpServer, self.server)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress stdlib request logging so query values never reach logs."""

        del format, args

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del message, explain
        self._send_json(
            code,
            _error_body("BAD_REQUEST", "Dashboard request could not be processed"),
            head_only=self.command == "HEAD",
        )

    def do_GET(self) -> None:
        self._handle_read(head_only=False)

    def do_HEAD(self) -> None:
        self._handle_read(head_only=True)

    def do_POST(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def do_TRACE(self) -> None:
        self._method_not_allowed()

    def do_CONNECT(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            _error_body("METHOD_NOT_ALLOWED", "Dashboard supports GET and HEAD only"),
            options=_ResponseOptions(
                content_type="application/json; charset=utf-8",
                extra_headers=(("Allow", "GET, HEAD"),),
            ),
        )

    def _valid_host(self) -> bool:
        values = self.headers.get_all("Host", failobj=[])
        if len(values) != 1:
            return False
        host = values[0].strip()
        if _LOCAL_HOST.fullmatch(host) is None and _LOCAL_IPV6_HOST.fullmatch(host) is None:
            return False
        raw_port = host.rpartition(":")[2]
        return not raw_port.isdigit() or 0 < int(raw_port) <= 65_535

    def _target(self) -> tuple[str, str]:
        if len(self.path) > MAX_REQUEST_TARGET_CHARS:
            raise DashboardApiError(414, "REQUEST_TARGET_TOO_LARGE", "Request target is too long")
        try:
            target = urlsplit(self.path)
        except ValueError as exc:
            raise DashboardApiError(400, "BAD_REQUEST", "Request target is malformed") from exc
        if target.scheme or target.netloc or target.fragment or not target.path.startswith("/"):
            raise DashboardApiError(400, "BAD_REQUEST", "Absolute request targets are refused")
        return target.path, target.query

    def _handle_read(self, *, head_only: bool) -> None:
        if not self._valid_host():
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                _error_body("HOST_REJECTED", "Dashboard accepts localhost Host values only"),
                head_only=head_only,
            )
            return
        try:
            path, query = self._target()
            if path.startswith("/api/"):
                response = self.dashboard_server.dashboard_api.dispatch(path, query)
                self._send_json(response.status, response.body, head_only=head_only)
                return
            self._send_static(path, query, head_only=head_only)
        except DashboardApiError as exc:
            response = exc.response()
            self._send_json(response.status, response.body, head_only=head_only)
        except Exception as exc:  # HTTP diagnostics must never expose internal failures.
            logger.error("Dashboard request failed safely (type=%s)", type(exc).__name__)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                _error_body("INTERNAL_ERROR", "Dashboard could not complete the request"),
                head_only=head_only,
            )

    def _send_static(self, path: str, query: str, *, head_only: bool) -> None:
        if query:
            raise DashboardApiError(400, "INVALID_QUERY", "Static assets do not accept parameters")
        asset = _STATIC_ASSETS.get(path)
        if asset is None:
            raise DashboardApiError(404, "NOT_FOUND", "Dashboard route was not found")
        relative_path, content_type = asset
        self._send_bytes(
            HTTPStatus.OK,
            _asset_bytes(relative_path),
            _ResponseOptions(content_type=content_type, head_only=head_only),
        )

    def _send_json(
        self,
        status: int,
        body: JsonObject,
        *,
        head_only: bool = False,
        options: _ResponseOptions | None = None,
    ) -> None:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response_options = options or _ResponseOptions(
            content_type="application/json; charset=utf-8",
            head_only=head_only,
        )
        self._send_bytes(
            status,
            encoded,
            response_options,
        )

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        options: _ResponseOptions,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", options.content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        for name, value in options.extra_headers:
            self.send_header(name, value)
        self.end_headers()
        if not options.head_only:
            self.wfile.write(body)


def build_dashboard_server(
    workspace: Path, *, port: int = DEFAULT_DASHBOARD_PORT
) -> DashboardHttpServer:
    """Build a loopback server without starting its foreground loop."""

    if not 0 <= port <= 65_535:
        raise ValueError("Dashboard port must be between 0 and 65535")
    root = WorkspaceRoot(workspace.resolve())
    return DashboardHttpServer(port, DashboardApi(DiagnosticsService(root)))


def serve_dashboard(
    workspace: Path,
    *,
    port: int = DEFAULT_DASHBOARD_PORT,
    open_browser: bool = False,
) -> None:
    """Run the local operator dashboard in the foreground until Ctrl+C."""

    server = build_dashboard_server(workspace, port=port)
    bound_port = int(server.server_address[1])
    url = f"http://{DASHBOARD_HOST}:{bound_port}"
    print(f"Ferumind Operator Console: {url}")
    print("Read-only and loopback-only. Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nFerumind dashboard stopped.")
    finally:
        server.server_close()
