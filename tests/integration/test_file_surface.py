"""Protocol-level tests for the generic file surface (spec-mcp §5.4).

These drive a real in-process MCP client against the assembled server, so
they assert what a host actually receives — serialized ``tools/call``
content blocks and ``resources/read`` contents — rather than what the
Python function happened to return.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    ErrorData,
    ImageContent,
    ReadResourceResult,
    ResourceLink,
    TextContent,
    TextResourceContents,
)
from PIL import Image

from ferumind.core.file_uri import build_file_uri
from ferumind.core.paths import WorkspaceRoot, contained_project_root
from ferumind.core.renditions import DEFAULT_IMAGE_EDGE, MAX_IMAGE_RENDITION_BYTES

PROJECT = "demo"


@pytest.fixture
def project_files(workspace: WorkspaceRoot, large_photo_bytes: bytes) -> dict[str, Path]:
    """A project holding the file shapes this surface has to handle."""
    from ferumind.core.writes import create_project
    from ferumind.db.database import Database

    db = Database(workspace / ".ferumind" / "ferumind.sqlite")
    db.init_schema()
    conn = db.get_connection()
    try:
        create_project(conn, workspace, key=PROJECT, title="Demo")
    finally:
        conn.close()

    root = contained_project_root(workspace, PROJECT)
    created: dict[str, Path] = {}

    photo_dir = root / "library" / "trip photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    photo = photo_dir / "café föto.jpg"
    photo.write_bytes(large_photo_bytes)
    created["photo"] = photo

    small = root / "inbox" / "thumb.png"
    small.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (64, 48), (10, 20, 30, 0)).save(small, format="PNG")
    created["png"] = small

    pdf = root / "library" / "report.pdf"
    pdf.write_bytes(b"%PDF-1.7\n" + bytes(range(256)) * 4 + b"\n%%EOF\n")
    created["pdf"] = pdf

    text = root / "canvases" / "notes.txt"
    text.parent.mkdir(parents=True, exist_ok=True)
    text.write_text("héllo wörld\n" * 40, encoding="utf-8")
    created["text"] = text

    return created


@asynccontextmanager
async def _client(workspace: WorkspaceRoot) -> AsyncGenerator[ClientSession]:
    from ferumind.mcp import server, tool_context

    tool_context.reset_tool_context()
    tool_context.init_tool_context(Path(workspace))
    server.register_all_tools()
    lowlevel = server.mcp._mcp_server  # pyright: ignore[reportPrivateUsage]
    try:
        async with create_connected_server_and_client_session(lowlevel) as session:
            await session.initialize()
            yield session
    finally:
        tool_context.reset_tool_context()


@pytest.fixture
def run_session(workspace: WorkspaceRoot) -> Callable[[Any], Any]:
    """Run one coroutine against a connected in-process client session."""

    def run(body: Any) -> Any:
        async def main() -> Any:
            async with _client(workspace) as session:
                return await body(session)

        return anyio.run(main)

    return run


def envelope(result: CallToolResult) -> dict[str, Any]:
    structured = result.structuredContent
    assert isinstance(structured, dict)
    return structured


def data_of(result: CallToolResult) -> dict[str, Any]:
    env = envelope(result)
    assert env["ok"] is True, env
    return cast("dict[str, Any]", env["data"])


def resource_error(run_session: Callable[[Any], Any], uri: str) -> ErrorData:
    """Read a resource expecting failure, returning the JSON-RPC error.

    The error is captured inside the coroutine: anyio re-raises anything
    escaping the session task group as an ``ExceptionGroup``, which would
    hide the ``ErrorData`` these tests are actually about.
    """

    async def body(session: ClientSession) -> ErrorData:
        try:
            await session.read_resource(cast("Any", uri))
        except McpError as exc:
            return exc.error
        raise AssertionError(f"expected {uri} to be rejected")

    return cast("ErrorData", run_session(body))


class TestProtocolSurface:
    def test_resource_capability_is_advertised(self, run_session: Callable[[Any], Any]) -> None:
        async def body(session: ClientSession) -> Any:
            return await session.initialize()

        result = run_session(body)
        assert result.capabilities.resources is not None

    def test_tools_are_advertised_with_schemas_and_teaching_descriptions(
        self, run_session: Callable[[Any], Any]
    ) -> None:
        async def body(session: ClientSession) -> Any:
            return await session.list_tools()

        listed = run_session(body)
        by_name = {tool.name: tool for tool in listed.tools}

        assert "list_files" in by_name
        assert "read_file" in by_name
        # Existing surface is untouched.
        for existing in ("read_document", "search_project", "upload_library_file"):
            assert existing in by_name

        list_files = by_name["list_files"]
        assert set(list_files.inputSchema["properties"]) == {
            "project",
            "path_prefix",
            "query",
            "mime_type",
            "extension",
            "include_markdown",
            "include_sidecars",
            "limit",
            "cursor",
        }
        assert list_files.inputSchema["required"] == ["project"]
        assert list_files.annotations is not None
        assert list_files.annotations.readOnlyHint is True

        read_file = by_name["read_file"]
        assert set(read_file.inputSchema["properties"]) == {
            "project",
            "path",
            "max_image_edge",
            "image_quality",
            "text_offset",
            "max_text_chars",
        }
        assert read_file.annotations is not None
        assert read_file.annotations.readOnlyHint is True

        # The descriptions have to teach the workflow and the honest limits.
        assert "read_file" in (list_files.description or "")
        assert "NOT indexed" in (list_files.description or "")
        for phrase in ("resource_only", "OCR", "read_document"):
            assert phrase in (read_file.description or "")

    @pytest.mark.usefixtures("project_files")
    def test_resource_template_is_advertised_but_resources_are_not_enumerated(
        self, run_session: Callable[[Any], Any]
    ) -> None:
        async def body(session: ClientSession) -> Any:
            return (
                await session.list_resource_templates(),
                await session.list_resources(),
            )

        templates, resources = run_session(body)
        uris = [template.uriTemplate for template in templates.resourceTemplates]
        assert "ferumind://file/{project}/{encoded_path}" in uris
        # A project can hold thousands of files; discovery is list_files.
        assert resources.resources == []


class TestListFilesOverProtocol:
    @pytest.mark.usefixtures("project_files")
    def test_discovers_arbitrary_nested_files(self, run_session: Callable[[Any], Any]) -> None:
        async def body(session: ClientSession) -> CallToolResult:
            return await session.call_tool("list_files", {"project": PROJECT})

        data = data_of(run_session(body))
        found = {entry["path"]: entry for entry in data["files"]}
        assert set(found) == {
            "canvases/notes.txt",
            "inbox/thumb.png",
            "library/report.pdf",
            "library/trip photos/café föto.jpg",
        }
        photo = found["library/trip photos/café föto.jpg"]
        assert photo["mime_type"] == "image/jpeg"
        assert photo["context_support"] == "image"
        assert photo["resource_uri"].startswith("ferumind://file/demo/")
        assert found["library/report.pdf"]["context_support"] == "resource_only"

    @pytest.mark.usefixtures("project_files")
    def test_no_absolute_path_appears_anywhere_in_the_response(
        self, run_session: Callable[[Any], Any], workspace: WorkspaceRoot
    ) -> None:
        async def body(session: ClientSession) -> CallToolResult:
            return await session.call_tool("list_files", {"project": PROJECT})

        serialized = run_session(body).model_dump_json()
        assert str(workspace) not in serialized

    def test_unknown_project_is_rejected(self, run_session: Callable[[Any], Any]) -> None:
        async def body(session: ClientSession) -> CallToolResult:
            return await session.call_tool("list_files", {"project": "no-such-project"})

        env = envelope(run_session(body))
        assert env["ok"] is False
        assert env["error_code"] == "PROJECT_NOT_FOUND"

    @pytest.mark.usefixtures("project_files")
    def test_filters_and_pagination_travel_over_the_wire(
        self, run_session: Callable[[Any], Any]
    ) -> None:
        async def body(session: ClientSession) -> tuple[CallToolResult, CallToolResult]:
            first = await session.call_tool("list_files", {"project": PROJECT, "limit": 2})
            structured = cast("dict[str, Any]", first.structuredContent)
            cursor = structured["data"]["next_cursor"]
            second = await session.call_tool(
                "list_files", {"project": PROJECT, "limit": 2, "cursor": cursor}
            )
            return first, second

        first, second = run_session(body)
        first_data, second_data = data_of(first), data_of(second)
        assert first_data["count"] == 2
        assert first_data["has_more"] is True
        first_paths = [entry["path"] for entry in first_data["files"]]
        second_paths = [entry["path"] for entry in second_data["files"]]
        assert set(first_paths).isdisjoint(second_paths)
        assert sorted(first_paths + second_paths) == first_paths + second_paths


class TestReadFileOverProtocol:
    def test_jpeg_returns_a_real_image_block_and_resource_link(
        self, run_session: Callable[[Any], Any], project_files: dict[str, Path]
    ) -> None:
        original = project_files["photo"].read_bytes()
        assert len(original) > 4_000_000

        async def body(session: ClientSession) -> CallToolResult:
            return await session.call_tool(
                "read_file",
                {"project": PROJECT, "path": "library/trip photos/café föto.jpg"},
            )

        result = run_session(body)
        kinds = [block.type for block in result.content]
        assert kinds == ["text", "image", "resource_link"]

        image = result.content[1]
        assert isinstance(image, ImageContent)
        assert image.mimeType == "image/jpeg"
        rendition_bytes = base64.b64decode(image.data)
        # A ~5 MB original must not travel inline as the tool's image.
        assert len(rendition_bytes) <= MAX_IMAGE_RENDITION_BYTES
        with Image.open(__import__("io").BytesIO(rendition_bytes)) as decoded:
            assert max(decoded.size) <= DEFAULT_IMAGE_EDGE

        link = result.content[2]
        assert isinstance(link, ResourceLink)
        assert str(link.uri) == build_file_uri(PROJECT, "library/trip photos/café föto.jpg")
        assert link.mimeType == "image/jpeg"

        data = data_of(result)
        assert data["original"]["size_bytes"] == len(original)
        assert data["original"]["width"] == 4032
        assert data["rendition"]["width"] <= DEFAULT_IMAGE_EDGE
        assert data["rendition"]["size_bytes"] == len(rendition_bytes)
        assert data["rendition"]["size_limit_bytes"] == MAX_IMAGE_RENDITION_BYTES

        # Bound the complete serialized result, not just the JPEG. Base64,
        # summaries, structured metadata, and links all count at the host
        # boundary. This leaves substantial margin below Claude.ai/Desktop's
        # documented approximate 150,000-character limit and the smaller
        # boundary observed in ChatGPT web testing.
        assert len(result.model_dump_json()) < 96 * 1024

        # The original is untouched by the read.
        assert project_files["photo"].read_bytes() == original

    @pytest.mark.usefixtures("project_files")
    def test_image_base64_appears_only_in_the_image_block(
        self, run_session: Callable[[Any], Any]
    ) -> None:
        async def body(session: ClientSession) -> CallToolResult:
            return await session.call_tool(
                "read_file",
                {"project": PROJECT, "path": "library/trip photos/café föto.jpg"},
            )

        result = run_session(body)
        image = result.content[1]
        assert isinstance(image, ImageContent)
        payload = image.data

        assert payload not in json.dumps(result.structuredContent)
        for block in result.content:
            if isinstance(block, TextContent):
                assert payload not in block.text
        assert payload not in json.dumps(result.meta or {})
        # A meaningful sample of the payload must not be hiding anywhere else.
        sample = payload[:200]
        assert json.dumps(result.structuredContent).count(sample) == 0

    @pytest.mark.usefixtures("project_files")
    def test_png_with_transparency_keeps_a_png_rendition(
        self, run_session: Callable[[Any], Any]
    ) -> None:
        async def body(session: ClientSession) -> CallToolResult:
            return await session.call_tool(
                "read_file", {"project": PROJECT, "path": "inbox/thumb.png"}
            )

        result = run_session(body)
        image = result.content[1]
        assert isinstance(image, ImageContent)
        assert image.mimeType == "image/png"
        data = data_of(result)
        # Never upscaled: a 64x48 source stays 64x48.
        assert data["rendition"]["width"] == 64
        assert data["rendition"]["resized"] is False

    @pytest.mark.usefixtures("project_files")
    def test_image_parameter_bounds_are_rejected_by_the_schema(
        self, run_session: Callable[[Any], Any]
    ) -> None:
        async def body(session: ClientSession) -> tuple[CallToolResult, CallToolResult]:
            path = "library/trip photos/café föto.jpg"
            too_big = await session.call_tool(
                "read_file", {"project": PROJECT, "path": path, "max_image_edge": 99999}
            )
            bad_quality = await session.call_tool(
                "read_file", {"project": PROJECT, "path": path, "image_quality": 1}
            )
            return too_big, bad_quality

        too_big, bad_quality = run_session(body)
        assert envelope(too_big)["error_code"] == "VALIDATION_ERROR"
        assert envelope(bad_quality)["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.usefixtures("project_files")
    def test_text_file_returns_bounded_text_and_a_link(
        self, run_session: Callable[[Any], Any]
    ) -> None:
        async def body(session: ClientSession) -> CallToolResult:
            return await session.call_tool(
                "read_file",
                {"project": PROJECT, "path": "canvases/notes.txt", "max_text_chars": 20},
            )

        result = run_session(body)
        kinds = [block.type for block in result.content]
        assert kinds == ["text", "text", "resource_link"]
        body_block = result.content[1]
        assert isinstance(body_block, TextContent)
        assert body_block.text == "héllo wörld\nhéllo wö"

        data = data_of(result)
        assert data["text"]["truncated"] is True
        assert data["text"]["next_offset"] == 20
        assert data["representation"] == "text"

    def test_pdf_is_resource_only_with_no_inline_binary(
        self, run_session: Callable[[Any], Any], project_files: dict[str, Path]
    ) -> None:
        async def body(session: ClientSession) -> CallToolResult:
            return await session.call_tool(
                "read_file", {"project": PROJECT, "path": "library/report.pdf"}
            )

        result = run_session(body)
        kinds = [block.type for block in result.content]
        assert kinds == ["text", "resource_link"]
        summary = result.content[0]
        assert isinstance(summary, TextContent)
        assert "not been read" in summary.text

        data = data_of(result)
        assert data["representation"] == "resource_only"
        assert "rendition" not in data
        # No base64 of the PDF anywhere in the tool result.
        encoded = base64.b64encode(project_files["pdf"].read_bytes()).decode()
        assert encoded not in result.model_dump_json()

    @pytest.mark.usefixtures("project_files")
    def test_missing_and_traversal_paths_produce_ferumind_error_codes(
        self, run_session: Callable[[Any], Any]
    ) -> None:
        async def body(session: ClientSession) -> tuple[CallToolResult, CallToolResult]:
            missing = await session.call_tool(
                "read_file", {"project": PROJECT, "path": "library/nope.jpg"}
            )
            traversal = await session.call_tool(
                "read_file", {"project": PROJECT, "path": "../../../etc/passwd"}
            )
            return missing, traversal

        missing, traversal = run_session(body)
        assert envelope(missing)["error_code"] == "FILE_NOT_FOUND"
        assert envelope(traversal)["error_code"] == "WORKSPACE_MISMATCH"


class TestResourceReadsOverProtocol:
    def test_jpeg_resource_round_trips_byte_for_byte(
        self, run_session: Callable[[Any], Any], project_files: dict[str, Path]
    ) -> None:
        uri = build_file_uri(PROJECT, "library/trip photos/café föto.jpg")

        async def body(session: ClientSession) -> ReadResourceResult:
            return await session.read_resource(cast("Any", uri))

        result = run_session(body)
        content = result.contents[0]
        assert isinstance(content, BlobResourceContents)
        assert content.mimeType == "image/jpeg"
        assert base64.b64decode(content.blob) == project_files["photo"].read_bytes()

    def test_pdf_resource_round_trips_byte_for_byte(
        self, run_session: Callable[[Any], Any], project_files: dict[str, Path]
    ) -> None:
        uri = build_file_uri(PROJECT, "library/report.pdf")

        async def body(session: ClientSession) -> ReadResourceResult:
            return await session.read_resource(cast("Any", uri))

        content = run_session(body).contents[0]
        assert isinstance(content, BlobResourceContents)
        assert content.mimeType == "application/pdf"
        assert base64.b64decode(content.blob) == project_files["pdf"].read_bytes()

    def test_utf8_text_uses_text_resource_contents(
        self, run_session: Callable[[Any], Any], project_files: dict[str, Path]
    ) -> None:
        uri = build_file_uri(PROJECT, "canvases/notes.txt")

        async def body(session: ClientSession) -> ReadResourceResult:
            return await session.read_resource(cast("Any", uri))

        content = run_session(body).contents[0]
        assert isinstance(content, TextResourceContents)
        assert content.mimeType == "text/plain"
        assert content.text == project_files["text"].read_text(encoding="utf-8")

    def test_the_uri_from_read_file_is_directly_readable(
        self, run_session: Callable[[Any], Any], project_files: dict[str, Path]
    ) -> None:
        """The acceptance path: list -> read_file -> resources/read."""

        async def body(session: ClientSession) -> tuple[str, ReadResourceResult]:
            listed = await session.call_tool(
                "list_files", {"project": PROJECT, "mime_type": "image/jpeg"}
            )
            structured = cast("dict[str, Any]", listed.structuredContent)
            path = structured["data"]["files"][0]["path"]
            read = await session.call_tool("read_file", {"project": PROJECT, "path": path})
            link = read.content[2]
            assert isinstance(link, ResourceLink)
            return path, await session.read_resource(cast("Any", str(link.uri)))

        path, resource = run_session(body)
        assert path == "library/trip photos/café föto.jpg"
        content = resource.contents[0]
        assert isinstance(content, BlobResourceContents)
        assert base64.b64decode(content.blob) == project_files["photo"].read_bytes()

    @pytest.mark.usefixtures("project_files")
    def test_response_never_includes_the_absolute_server_path(
        self, run_session: Callable[[Any], Any], workspace: WorkspaceRoot
    ) -> None:
        uri = build_file_uri(PROJECT, "library/report.pdf")

        async def body(session: ClientSession) -> ReadResourceResult:
            return await session.read_resource(cast("Any", uri))

        serialized = run_session(body).model_dump_json()
        assert str(workspace) not in serialized

    @pytest.mark.parametrize(
        ("uri", "expected_code"),
        [
            ("ferumind://file/demo/!!!not-base64!!!", "VALIDATION_ERROR"),
            ("ferumind://file/demo", "VALIDATION_ERROR"),
            ("ferumind://file/demo/aGk/extra", "VALIDATION_ERROR"),
            ("ferumind://file/UNKNOWN/aGk", "VALIDATION_ERROR"),
        ],
    )
    @pytest.mark.usefixtures("project_files")
    def test_malformed_uris_are_rejected(
        self, run_session: Callable[[Any], Any], uri: str, expected_code: str
    ) -> None:
        error = resource_error(run_session, uri)
        assert cast("dict[str, Any]", error.data)["error_code"] == expected_code

    @pytest.mark.usefixtures("project_files")
    def test_traversal_uri_is_rejected(self, run_session: Callable[[Any], Any]) -> None:
        error = resource_error(run_session, build_file_uri(PROJECT, "../../../etc/passwd"))
        assert cast("dict[str, Any]", error.data)["error_code"] == "WORKSPACE_MISMATCH"
        # The refusal must not disclose where the workspace actually lives.
        assert "/" not in error.message or "etc/passwd" not in error.message

    @pytest.mark.usefixtures("project_files")
    def test_unknown_project_uri_is_rejected(self, run_session: Callable[[Any], Any]) -> None:
        error = resource_error(run_session, build_file_uri("nosuch", "library/report.pdf"))
        assert cast("dict[str, Any]", error.data)["error_code"] == "PROJECT_NOT_FOUND"

    @pytest.mark.usefixtures("project_files")
    def test_missing_file_uri_is_rejected(self, run_session: Callable[[Any], Any]) -> None:
        error = resource_error(run_session, build_file_uri(PROJECT, "library/ghost.jpg"))
        assert cast("dict[str, Any]", error.data)["error_code"] == "FILE_NOT_FOUND"

    @pytest.mark.usefixtures("project_files")
    def test_symlinked_resource_is_rejected(
        self, run_session: Callable[[Any], Any], workspace: WorkspaceRoot, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        root = contained_project_root(workspace, PROJECT)
        (root / "library" / "link.txt").symlink_to(outside)

        error = resource_error(run_session, build_file_uri(PROJECT, "library/link.txt"))
        assert cast("dict[str, Any]", error.data)["error_code"] == "WORKSPACE_MISMATCH"

    @pytest.mark.usefixtures("project_files")
    def test_file_over_the_cap_fails_explicitly(
        self, run_session: Callable[[Any], Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ferumind.core import file_reads

        monkeypatch.setattr(file_reads, "MAX_RESOURCE_READ_BYTES", 32)

        error = resource_error(run_session, build_file_uri(PROJECT, "library/report.pdf"))
        payload = cast("dict[str, Any]", error.data)
        assert payload["error_code"] == "FILE_TOO_LARGE"
        assert payload["size_bytes"] > 32
        assert payload["limit_bytes"] == 32


class TestObservationTelemetry:
    @pytest.mark.usefixtures("project_files")
    def test_file_calls_and_resource_reads_record_metadata_only(
        self, run_session: Callable[[Any], Any], workspace: WorkspaceRoot
    ) -> None:
        from ferumind.core.observations import list_observations
        from ferumind.db.database import Database
        from ferumind.mcp.observation import apply_observation_to_all_tools
        from ferumind.mcp.server import mcp

        apply_observation_to_all_tools(mcp)

        async def body(session: ClientSession) -> None:
            await session.call_tool("list_files", {"project": PROJECT})
            await session.call_tool(
                "read_file",
                {"project": PROJECT, "path": "library/trip photos/café föto.jpg"},
            )
            await session.read_resource(cast("Any", build_file_uri(PROJECT, "library/report.pdf")))

        run_session(body)

        db = Database(workspace / ".ferumind" / "ferumind.sqlite")
        conn = db.get_connection()
        try:
            observed = {row.tool_name: row for row in list_observations(conn, limit=50)}
        finally:
            conn.close()

        listing = observed["list_files"]
        assert json.loads(listing.context_metrics_json)["count"] == 4

        reading = observed["read_file"]
        metrics = json.loads(reading.context_metrics_json)
        assert metrics["representation"] == "image"
        assert metrics["original_size_bytes"] > metrics["rendition_size_bytes"]

        resource = observed["resources/read"]
        resource_metrics = json.loads(resource.context_metrics_json)
        assert resource_metrics["mime_type"] == "application/pdf"
        assert resource_metrics["kind"] == "blob"

        # Telemetry is metadata only: no path values, no content.
        for record in (listing, reading, resource):
            blob = record.context_metrics_json + record.argument_keys_json
            assert "café" not in blob
            assert "%PDF" not in blob
