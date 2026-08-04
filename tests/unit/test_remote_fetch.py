"""Tests for the SSRF-hardened remote fetch used by ChatGPT file-reference uploads."""

from __future__ import annotations

import gzip
import socket
from collections.abc import Callable

import httpx
import pytest

from lattice.core.errors import (
    DownloadFailedError,
    DownloadTimeoutError,
    FileTooLargeError,
    TooManyRedirectsError,
    UnsafeUrlError,
)
from lattice.core.remote_fetch import (
    Resolver,
    _default_resolve,  # pyright: ignore[reportPrivateUsage]
    _is_unsafe_ip,  # pyright: ignore[reportPrivateUsage]
    fetch_remote_file,
)

# Justification for reportPrivateUsage: this SSRF-hardening module's IP
# classification and default resolver are internal helpers with no public
# surface of their own; they are security-critical and require direct
# adversarial unit tests (see AGENTS.md "Path-security code requires
# adversarial tests" — the same standard applies to SSRF defenses), not just
# indirect coverage through fetch_remote_file's public behavior.

PUBLIC_IP = "93.184.216.34"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _resolve_to(*ips: str) -> Resolver:
    """Build a typed Resolver stub that ignores the hostname and returns fixed IPs."""

    def resolve(_host: str) -> list[str]:
        return list(ips)

    return resolve


def _static_response(
    content: bytes = b"x", status: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a typed MockTransport handler that ignores the request and returns a fixed response."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=content)

    return handler


class TestIsUnsafeIp:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",
            "::1",
            "10.0.0.5",
            "192.168.1.1",
            "172.16.0.1",
            "169.254.1.1",
            "169.254.169.254",  # AWS/GCP/Azure metadata
            "224.0.0.1",  # multicast
            "240.0.0.1",  # reserved
            "0.0.0.0",  # unspecified
            "100.64.0.1",  # shared carrier-grade NAT
            "::ffff:127.0.0.1",  # IPv4-mapped IPv6 loopback
            "100.100.100.200",  # Alibaba Cloud metadata (explicit denylist)
            "fd00:ec2::254",  # AWS IMDSv2 IPv6 (explicit denylist)
            "not-an-ip",
        ],
    )
    def test_blocks_unsafe_addresses(self, ip: str) -> None:
        assert _is_unsafe_ip(ip) is True

    @pytest.mark.parametrize("ip", [PUBLIC_IP, "8.8.8.8", "2606:4700:4700::1111"])
    def test_allows_public_addresses(self, ip: str) -> None:
        assert _is_unsafe_ip(ip) is False


class TestDefaultResolve:
    def test_resolution_failure_raises_unsafe_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise socket.gaierror("nope")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        with pytest.raises(UnsafeUrlError):
            _default_resolve("nonexistent.invalid")


class TestFetchRemoteFile:
    def test_happy_path_downloads_bytes(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["host"] == "example.com"
            assert request.url.host == PUBLIC_IP
            return httpx.Response(200, content=b"hello world")

        data = fetch_remote_file(
            "https://example.com/file.bin",
            max_bytes=1000,
            client=_client(handler),
            resolve=_resolve_to(PUBLIC_IP),
        )
        assert data == b"hello world"

    def test_non_https_rejected(self) -> None:
        with pytest.raises(UnsafeUrlError):
            fetch_remote_file(
                "http://example.com/x",
                max_bytes=1000,
                client=_client(_static_response()),
                resolve=_resolve_to(PUBLIC_IP),
            )

    def test_url_with_no_hostname_rejected(self) -> None:
        with pytest.raises(UnsafeUrlError):
            fetch_remote_file(
                "https:///no-host",
                max_bytes=1000,
                client=_client(_static_response()),
                resolve=_resolve_to(PUBLIC_IP),
            )

    @pytest.mark.parametrize(
        "url",
        [
            "https://user:password@example.com/file",
            "https://example.com:8443/file",
            "https://example.com:notaport/file",
        ],
    )
    def test_credentials_and_nonstandard_or_invalid_ports_rejected(self, url: str) -> None:
        with pytest.raises(UnsafeUrlError):
            fetch_remote_file(
                url,
                max_bytes=1000,
                client=_client(_static_response()),
                resolve=_resolve_to(PUBLIC_IP),
            )

    @pytest.mark.parametrize("bad_ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254"])
    def test_unsafe_resolved_address_rejected(self, bad_ip: str) -> None:
        with pytest.raises(UnsafeUrlError):
            fetch_remote_file(
                "https://internal.example/x",
                max_bytes=1000,
                client=_client(_static_response()),
                resolve=_resolve_to(bad_ip),
            )

    def test_oversized_response_stopped_while_streaming(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 5000)

        with pytest.raises(FileTooLargeError):
            fetch_remote_file(
                "https://example.com/big",
                max_bytes=100,
                client=_client(handler),
                resolve=_resolve_to(PUBLIC_IP),
            )

    def test_misleading_content_length_cannot_bypass_size_cap(self) -> None:
        """A lying (too-small) Content-Length header must not bypass the real cap."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-length": "10"}, content=b"x" * 5000)

        with pytest.raises(FileTooLargeError):
            fetch_remote_file(
                "https://example.com/lied-about-size",
                max_bytes=100,
                client=_client(handler),
                resolve=_resolve_to(PUBLIC_IP),
            )

    def test_missing_content_length_cannot_bypass_size_cap(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            response = httpx.Response(200, content=b"x" * 5000)
            del response.headers["content-length"]
            return response

        with pytest.raises(FileTooLargeError):
            fetch_remote_file(
                "https://example.com/no-content-length",
                max_bytes=100,
                client=_client(handler),
                resolve=_resolve_to(PUBLIC_IP),
            )

    def test_compressed_response_is_rejected_without_decompression(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                content=gzip.compress(b"compressed"),
            )

        with pytest.raises(DownloadFailedError, match="identity encoding"):
            fetch_remote_file(
                "https://example.com/compressed",
                max_bytes=100,
                client=_client(handler),
                resolve=_resolve_to(PUBLIC_IP),
            )

    def test_redirect_is_followed(self) -> None:
        hosts_seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            hosts_seen.append(request.headers["host"])
            if request.headers["host"] == "a.example.com":
                return httpx.Response(302, headers={"location": "https://b.example.com/next"})
            return httpx.Response(200, content=b"final content")

        def resolve(host: str) -> list[str]:
            return {"a.example.com": ["93.184.216.1"], "b.example.com": ["93.184.216.2"]}[host]

        data = fetch_remote_file(
            "https://a.example.com/start", max_bytes=1000, client=_client(handler), resolve=resolve
        )
        assert data == b"final content"
        assert hosts_seen == ["a.example.com", "b.example.com"]

    def test_redirect_to_unsafe_host_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://evil.internal/x"})

        def resolve(host: str) -> list[str]:
            return ["93.184.216.1"] if host == "safe.example.com" else ["127.0.0.1"]

        with pytest.raises(UnsafeUrlError):
            fetch_remote_file(
                "https://safe.example.com/start",
                max_bytes=1000,
                client=_client(handler),
                resolve=resolve,
            )

    def test_redirect_missing_location_header_fails(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302)

        with pytest.raises(DownloadFailedError):
            fetch_remote_file(
                "https://example.com/start",
                max_bytes=1000,
                client=_client(handler),
                resolve=_resolve_to(PUBLIC_IP),
            )

    def test_excessive_redirects_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://loop.example.com/x"})

        with pytest.raises(TooManyRedirectsError):
            fetch_remote_file(
                "https://loop.example.com/start",
                max_bytes=1000,
                client=_client(handler),
                resolve=_resolve_to(PUBLIC_IP),
                max_redirects=3,
            )

    def test_non_2xx_response_is_download_failed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b"not found")

        with pytest.raises(DownloadFailedError):
            fetch_remote_file(
                "https://example.com/missing",
                max_bytes=1000,
                client=_client(handler),
                resolve=_resolve_to(PUBLIC_IP),
            )

    def test_connection_error_is_download_failed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with pytest.raises(DownloadFailedError):
            fetch_remote_file(
                "https://example.com/unreachable",
                max_bytes=1000,
                client=_client(handler),
                resolve=_resolve_to(PUBLIC_IP),
            )

    def test_read_timeout_is_download_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        with pytest.raises(DownloadTimeoutError):
            fetch_remote_file(
                "https://example.com/slow",
                max_bytes=1000,
                client=_client(handler),
                resolve=_resolve_to(PUBLIC_IP),
            )

    def test_total_wall_clock_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"some bytes")

        with pytest.raises(DownloadTimeoutError):
            fetch_remote_file(
                "https://example.com/slow-trickle",
                max_bytes=1000,
                client=_client(handler),
                resolve=_resolve_to(PUBLIC_IP),
                total_timeout=-0.001,
            )

    def test_error_messages_never_contain_the_download_url(self) -> None:
        """Temporary URLs (often carrying auth material) must never be logged/persisted."""
        secret_url = "https://evil.internal/path?token=super-secret-value"

        def resolve(host: str) -> list[str]:
            return ["127.0.0.1"]

        with pytest.raises(UnsafeUrlError) as excinfo:
            fetch_remote_file(
                secret_url,
                max_bytes=1000,
                client=_client(_static_response()),
                resolve=resolve,
            )
        assert "super-secret-value" not in str(excinfo.value)
        assert "super-secret-value" not in str(excinfo.value.details)
