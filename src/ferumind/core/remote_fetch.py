"""SSRF-hardened streaming fetch for host-supplied file references.

Backs ``upload_library_file(s)_from_chatgpt`` (``core.upload_writes``): ChatGPT's
``openai/fileParams`` MCP extension hands the server a temporary, authorized
``download_url`` rather than raw bytes or a server-local path. This module
downloads from that URL directly — streamed, size-capped, and validated —
so the model is never in the byte-transport path at all (unlike base64
tool arguments, where the model has to reproduce the bytes as text).

Defense in depth against SSRF:

- HTTPS only.
- The hostname (and every redirect target's hostname) is resolved and every
  returned address is checked against loopback/private/link-local/
  multicast/reserved/unspecified ranges plus a small explicit cloud
  metadata-service denylist, before any connection is attempted.
- The actual TCP connection targets the validated IP literally — not a
  second, separately-resolved hostname lookup — closing the DNS-rebinding
  TOCTOU window between validation and connection. SNI and the Host header
  still carry the original hostname (via httpx/httpcore's ``sni_hostname``
  request extension) so name-based virtual hosting and certificate
  hostname verification both still work correctly against the real domain.
- A small, fixed redirect budget; each hop is independently re-resolved
  and re-validated, never just the initial URL.
- Response bytes are streamed and counted as they arrive; the cap is
  enforced against actual received bytes, never against a client-supplied
  or server-supplied ``Content-Length`` header, which cannot be trusted.
- Connect/read timeouts and a total elapsed deadline. The operating system's
  DNS resolver is not independently interruptible, so production egress must
  also enforce an outer request deadline.

The temporary URL itself is never logged. ``httpx`` would emit the full
request URL at INFO, so ``core.logging_setup.PINNED_LOGGERS`` clamps that
logger to WARNING regardless of ``FERUMIND_LOG_LEVEL``. Only the hostname
appears in error messages, and the URL is never persisted anywhere — it exists
only for the duration of one ``fetch_remote_file`` call.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from typing import Final
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from ferumind.core.errors import (
    DownloadFailedError,
    DownloadTimeoutError,
    FileTooLargeError,
    TooManyRedirectsError,
    UnsafeUrlError,
)
from ferumind.core.types import JsonObject, JsonValue

#: hostname -> resolved IP address strings (injectable for tests; defaults
#: to a real DNS lookup).
Resolver = Callable[[str], list[str]]

DEFAULT_CONNECT_TIMEOUT: Final = 5.0
DEFAULT_READ_TIMEOUT: Final = 10.0
DEFAULT_TOTAL_TIMEOUT: Final = 30.0
DEFAULT_MAX_REDIRECTS: Final = 5
_RESPONSE_CHUNK_BYTES: Final = 64 * 1024
_ALLOWED_PORTS: Final[frozenset[int]] = frozenset({443})

_REDIRECT_STATUS_CODES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})

#: Cloud metadata-service addresses. Mostly already covered by
#: ``is_link_local``, but called out explicitly (defense in depth, and it
#: documents intent rather than relying on an incidental property overlap).
_METADATA_IPS: Final[frozenset[str]] = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure / DigitalOcean
        "100.100.100.200",  # Alibaba Cloud
        "fd00:ec2::254",  # AWS IMDSv2, IPv6
    }
)


def _default_resolve(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(
            f"Could not resolve host {hostname!r}: {exc}", details={"host": hostname}
        ) from exc
    # info[4] (sockaddr) is `tuple[str, int] | tuple[str, int, int, int]` in
    # typeshed; either way element 0 is the address string. str() is a no-op
    # here — it just gives the type checker the precise type already true at
    # runtime, without changing which addresses are returned.
    return [str(info[4][0]) for info in infos]


def _is_unsafe_ip(ip_str: str) -> bool:
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(
            ip_str.split("%", 1)[0]
        )
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if str(ip) in _METADATA_IPS:
        return True
    # ``is_global`` also rejects special-use ranges that are neither private
    # nor reserved on every supported Python version (for example
    # 100.64.0.0/10 shared carrier-grade NAT space).
    return (
        not ip.is_global
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
    )


def resolve_safe_ip(hostname: str, *, resolve: Resolver = _default_resolve) -> str:
    """Resolve *hostname* and return one address, only if every candidate is safe."""
    candidates = resolve(hostname)
    if not candidates:
        raise UnsafeUrlError(
            f"No addresses resolved for host {hostname!r}", details={"host": hostname}
        )
    for ip_str in candidates:
        if _is_unsafe_ip(ip_str):
            raise UnsafeUrlError(
                f"Host {hostname!r} resolves to a disallowed address",
                details={"host": hostname},
            )
    return candidates[0]


def _pin_request(url: str, *, resolve: Resolver) -> tuple[str, dict[str, str], dict[str, object]]:
    """Validate *url* and return (pinned_url, headers, extensions).

    ``pinned_url`` has its host replaced by the validated literal IP so the
    connection can't re-resolve to something different than what was just
    checked; ``headers``/``extensions`` restore the original hostname for
    the HTTP Host header and TLS SNI/certificate verification.
    """
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise UnsafeUrlError("URL is malformed") from exc
    if parsed.scheme != "https":
        raise UnsafeUrlError(
            "Only https:// URLs are allowed", details={"scheme": parsed.scheme or ""}
        )
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeUrlError("URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URLs containing credentials are not allowed")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise UnsafeUrlError("URL contains an invalid port") from exc
    if port not in _ALLOWED_PORTS:
        allowed_ports: list[JsonValue] = []
        allowed_ports.extend(sorted(_ALLOWED_PORTS))
        details: JsonObject = {
            "port": port,
            "allowed_ports": allowed_ports,
        }
        raise UnsafeUrlError(
            f"HTTPS port {port} is not allowed",
            details=details,
        )
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeUrlError("URL hostname is not valid IDNA") from exc

    ip = resolve_safe_ip(ascii_hostname, resolve=resolve)
    netloc = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
    pinned = urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
    host_header = ascii_hostname if port == 443 else f"{ascii_hostname}:{port}"
    return (
        pinned,
        {"Host": host_header, "Accept-Encoding": "identity"},
        {"sni_hostname": ascii_hostname},
    )


def fetch_remote_file(
    url: str,
    *,
    max_bytes: int,
    client: httpx.Client | None = None,
    resolve: Resolver = _default_resolve,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
) -> bytes:
    """Download *url*, streaming with a size cap and SSRF hardening (see module docstring).

    Raises :class:`UnsafeUrlError`, :class:`TooManyRedirectsError`,
    :class:`DownloadTimeoutError`, :class:`DownloadFailedError`, or
    :class:`FileTooLargeError` — never a raw ``httpx``/``socket`` exception.
    """
    owns_client = client is None
    http_client = client or httpx.Client(
        timeout=httpx.Timeout(
            connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout
        ),
        follow_redirects=False,
        trust_env=False,
    )
    try:
        current_url = url
        deadline = time.monotonic() + total_timeout
        for _ in range(max_redirects + 1):
            if time.monotonic() > deadline:
                raise DownloadTimeoutError(f"Download exceeded the {total_timeout}s total timeout")
            pinned_url, headers, extensions = _pin_request(current_url, resolve=resolve)
            if time.monotonic() > deadline:
                raise DownloadTimeoutError(f"Download exceeded the {total_timeout}s total timeout")
            try:
                with http_client.stream(
                    "GET",
                    pinned_url,
                    headers=headers,
                    extensions=extensions,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in _REDIRECT_STATUS_CODES:
                        location = response.headers.get("location")
                        if not location:
                            raise DownloadFailedError(
                                "Redirect response missing Location header",
                                details={"status_code": response.status_code},
                            )
                        current_url = urljoin(current_url, location)
                        continue
                    if not (200 <= response.status_code < 300):
                        raise DownloadFailedError(
                            f"Download failed with HTTP {response.status_code}",
                            details={"status_code": response.status_code},
                        )
                    content_encoding = response.headers.get("content-encoding", "identity").lower()
                    if content_encoding not in ("", "identity"):
                        raise DownloadFailedError(
                            "Download server ignored the identity encoding requirement",
                            details={"content_encoding": content_encoding},
                        )
                    buf = bytearray()
                    for chunk in response.iter_bytes(chunk_size=_RESPONSE_CHUNK_BYTES):
                        if time.monotonic() > deadline:
                            raise DownloadTimeoutError(
                                f"Download exceeded the {total_timeout}s total timeout"
                            )
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            raise FileTooLargeError(
                                f"Download exceeded the {max_bytes}-byte limit while streaming",
                                details={"max_bytes": max_bytes, "scope": "file"},
                            )
                    if time.monotonic() > deadline:
                        raise DownloadTimeoutError(
                            f"Download exceeded the {total_timeout}s total timeout"
                        )
                    return bytes(buf)
            except httpx.TimeoutException as exc:
                raise DownloadTimeoutError("Download timed out") from exc
            except httpx.HTTPError as exc:
                # httpx exception strings can include the full signed URL.
                # Keep temporary credentials out of both results and logs.
                raise DownloadFailedError("Download failed at the HTTP transport") from exc
        raise TooManyRedirectsError(
            f"Exceeded {max_redirects} redirects", details={"max_redirects": max_redirects}
        )
    finally:
        if owns_client:
            http_client.close()
