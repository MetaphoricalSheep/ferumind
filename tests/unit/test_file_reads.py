"""Tier 1 context reads and Tier 2 original-resource reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from ferumind.core.errors import (
    FileNotFoundFerumindError,
    FileTooLargeError,
    ValidationError,
)
from ferumind.core.file_reads import (
    MAX_RESOURCE_READ_BYTES,
    read_file_for_context,
    read_file_resource,
    resolve_project_file,
)
from ferumind.core.paths import PathSafetyError, WorkspaceRoot, contained_project_root
from ferumind.core.writes import MAX_UPLOAD_BYTES
from tests.unit.test_renditions import noisy_image


@pytest.fixture
def project_root(workspace: WorkspaceRoot, project: str) -> Path:
    return contained_project_root(workspace, project)


def write(root: Path, relative: str, content: bytes | str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        target.write_text(content, encoding="utf-8")
    else:
        target.write_bytes(content)
    return target


class TestResolution:
    def test_resolves_a_nested_file(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, "library/deep/nest/photo.jpg", b"bytes")

        resolved = resolve_project_file(workspace, project, "library/deep/nest/photo.jpg")

        assert resolved.path == "library/deep/nest/photo.jpg"
        assert resolved.mime_type == "image/jpeg"
        assert resolved.context_support == "image"
        assert resolved.resource_uri.startswith("ferumind://file/")

    @pytest.mark.parametrize(
        "path",
        [
            "../../../etc/passwd",
            "/etc/passwd",
            "library/../../escape.txt",
            "library\\windows.txt",
        ],
    )
    def test_traversal_and_absolute_paths_are_refused(
        self, workspace: WorkspaceRoot, project: str, path: str
    ) -> None:
        with pytest.raises(PathSafetyError):
            resolve_project_file(workspace, project, path)

    def test_symlink_is_refused(
        self, workspace: WorkspaceRoot, project: str, project_root: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (project_root / "library").mkdir(parents=True, exist_ok=True)
        (project_root / "library" / "link.txt").symlink_to(outside)

        with pytest.raises(PathSafetyError):
            resolve_project_file(workspace, project, "library/link.txt")

    def test_missing_file_reports_file_not_found(
        self, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(FileNotFoundFerumindError):
            resolve_project_file(workspace, project, "library/nope.jpg")

    def test_directory_is_refused(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        (project_root / "library" / "adir").mkdir(parents=True)
        with pytest.raises(ValidationError):
            resolve_project_file(workspace, project, "library/adir")

    def test_ferumind_internal_path_is_not_readable(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, ".ferumind/secret.bin", b"internal")
        with pytest.raises(FileNotFoundFerumindError):
            resolve_project_file(workspace, project, ".ferumind/secret.bin")

    def test_oversized_file_is_refused_with_sizes(
        self,
        workspace: WorkspaceRoot,
        project: str,
        project_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ferumind.core import file_reads

        monkeypatch.setattr(file_reads, "MAX_RESOURCE_READ_BYTES", 16)
        write(project_root, "library/big.bin", b"x" * 64)

        with pytest.raises(FileTooLargeError) as excinfo:
            resolve_project_file(workspace, project, "library/big.bin")
        details = excinfo.value.details
        assert details is not None
        assert details["size_bytes"] == 64
        assert details["limit_bytes"] == 16

    def test_resource_cap_is_not_below_the_upload_cap(self) -> None:
        assert MAX_RESOURCE_READ_BYTES >= MAX_UPLOAD_BYTES


class TestTransportDeliverability:
    """``resources/read`` must refuse what the caller's transport cannot carry.

    An oversized reply is worse than an error: the transport rejects the body
    and can tear down the connection carrying it, leaving the server
    unreachable for every later call.
    """

    def test_oversized_original_is_refused_with_a_pointer_to_read_file(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, "library/big.bin", b"x" * 9_000)

        with pytest.raises(FileTooLargeError) as excinfo:
            read_file_resource(workspace, project, "library/big.bin", max_response_bytes=10_000)

        details = excinfo.value.details
        assert details is not None
        # 9000 bytes base64-encodes to 12000, over the 10000-byte ceiling.
        assert details["encoded_estimate_bytes"] == 12_000
        assert details["max_response_bytes"] == 10_000
        assert details["max_original_bytes"] == 7_500
        assert details["recommended_tool"] == "read_file"

    def test_file_that_fits_once_encoded_is_served(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, "library/small.bin", b"x" * 6_000)

        content = read_file_resource(
            workspace, project, "library/small.bin", max_response_bytes=10_000
        )

        assert content.size_bytes == 6_000

    def test_no_limit_means_no_transport_guard(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, "library/big.bin", b"x" * 9_000)

        content = read_file_resource(workspace, project, "library/big.bin")

        assert content.size_bytes == 9_000

    def test_guard_runs_before_the_file_is_read(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        """A refused resource must not be pulled into memory first."""
        target = write(project_root, "library/big.bin", b"x" * 9_000)
        original_read_bytes = Path.read_bytes
        calls: list[Path] = []

        def tracking_read_bytes(self: Path) -> bytes:
            calls.append(self)
            return original_read_bytes(self)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Path, "read_bytes", tracking_read_bytes)
            with pytest.raises(FileTooLargeError):
                read_file_resource(workspace, project, "library/big.bin", max_response_bytes=10_000)

        assert target not in calls


class TestImageContext:
    def test_image_read_returns_a_rendition_and_leaves_the_original(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        (project_root / "library").mkdir(parents=True, exist_ok=True)
        source = project_root / "library" / "photo.jpg"
        noisy_image(3000, 2000).save(source, format="JPEG", quality=90)
        before = source.read_bytes()

        result = read_file_for_context(workspace, project, "library/photo.jpg")

        assert result.representation == "image"
        assert result.rendition is not None
        assert max(result.rendition.width, result.rendition.height) <= 1024
        assert result.rendition.size_bytes < len(before)
        assert source.read_bytes() == before

    def test_malformed_image_fails_as_a_ferumind_error(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, "library/broken.jpg", b"definitely not a jpeg")

        with pytest.raises(ValidationError):
            read_file_for_context(workspace, project, "library/broken.jpg")

    def test_sha256_describes_the_original_not_the_rendition(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        import hashlib

        (project_root / "library").mkdir(parents=True, exist_ok=True)
        source = project_root / "library" / "photo.png"
        noisy_image(300, 200).save(source, format="PNG")
        expected = hashlib.sha256(source.read_bytes()).hexdigest()

        result = read_file_for_context(workspace, project, "library/photo.png")

        assert result.sha256 == expected


class TestTextContext:
    def test_utf8_text_is_returned_whole_when_it_fits(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, "library/notes.txt", "héllo wörld\n")

        result = read_file_for_context(workspace, project, "library/notes.txt")

        assert result.representation == "text"
        assert result.text is not None
        assert result.text.text == "héllo wörld\n"
        assert result.text.truncated is False
        assert result.text.next_offset is None

    def test_pagination_reassembles_the_whole_file(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        content = "".join(f"line {index}\n" for index in range(500))
        write(project_root, "library/log.txt", content)

        chunks: list[str] = []
        offset = 0
        while True:
            result = read_file_for_context(
                workspace, project, "library/log.txt", text_offset=offset, max_text_chars=100
            )
            assert result.text is not None
            chunks.append(result.text.text)
            assert result.text.total_chars == len(content)
            if not result.text.truncated:
                break
            next_offset = result.text.next_offset
            assert next_offset == offset + result.text.returned_chars
            assert next_offset is not None
            offset = next_offset

        assert "".join(chunks) == content

    def test_truncation_metadata_is_accurate(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, "library/notes.txt", "abcdefghij")

        result = read_file_for_context(workspace, project, "library/notes.txt", max_text_chars=4)

        assert result.text is not None
        assert result.text.text == "abcd"
        assert result.text.returned_chars == 4
        assert result.text.total_chars == 10
        assert result.text.truncated is True
        assert result.text.next_offset == 4

    def test_multibyte_characters_are_never_split(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        content = "日本語テキスト" * 20
        write(project_root, "library/jp.txt", content)

        result = read_file_for_context(workspace, project, "library/jp.txt", max_text_chars=5)

        assert result.text is not None
        assert result.text.text == content[:5]
        assert result.text.text.encode("utf-8").decode("utf-8") == result.text.text

    def test_invalid_utf8_becomes_resource_only_not_mojibake(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, "library/broken.txt", b"valid \xff\xfe invalid")

        result = read_file_for_context(workspace, project, "library/broken.txt")

        assert result.representation == "resource_only"
        assert result.text is None
        assert result.reason == "not_valid_utf8"

    def test_offset_past_end_is_rejected(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, "library/short.txt", "abc")

        with pytest.raises(ValidationError):
            read_file_for_context(workspace, project, "library/short.txt", text_offset=99)

    def test_text_parameter_bounds_are_enforced(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, "library/notes.txt", "abc")

        with pytest.raises(ValidationError):
            read_file_for_context(workspace, project, "library/notes.txt", text_offset=-1)
        with pytest.raises(ValidationError):
            read_file_for_context(workspace, project, "library/notes.txt", max_text_chars=0)
        with pytest.raises(ValidationError):
            read_file_for_context(
                workspace, project, "library/notes.txt", max_text_chars=10_000_000
            )

    def test_markdown_is_served_as_text_and_flagged(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, "canvases/plan.md", "# Plan\n")

        result = read_file_for_context(workspace, project, "canvases/plan.md")

        assert result.representation == "text"
        assert result.file.is_markdown is True


class TestResourceOnly:
    def test_pdf_is_resource_only_with_no_inline_content(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, "library/report.pdf", b"%PDF-1.4\nbody\n")

        result = read_file_for_context(workspace, project, "library/report.pdf")

        assert result.representation == "resource_only"
        assert result.rendition is None
        assert result.text is None
        assert result.file.resource_uri

    def test_unknown_binary_is_not_decoded_as_text(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, "library/blob.dat", b"\x00\x01\x02plain-looking")

        result = read_file_for_context(workspace, project, "library/blob.dat")

        assert result.representation == "resource_only"
        assert result.text is None


class TestOriginalResourceReads:
    def test_binary_resource_is_byte_identical(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        (project_root / "library").mkdir(parents=True, exist_ok=True)
        source = project_root / "library" / "photo.jpg"
        noisy_image(900, 700).save(source, format="JPEG", quality=92)
        original = source.read_bytes()

        content = read_file_resource(workspace, project, "library/photo.jpg")

        assert content.blob == original
        assert content.text is None
        assert content.mime_type == "image/jpeg"
        assert content.size_bytes == len(original)

    def test_pdf_resource_is_byte_identical(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        payload = b"%PDF-1.7\n" + bytes(range(256)) * 8 + b"\n%%EOF\n"
        write(project_root, "library/report.pdf", payload)

        content = read_file_resource(workspace, project, "library/report.pdf")

        assert content.blob == payload
        assert content.mime_type == "application/pdf"

    def test_utf8_text_resource_uses_text_content(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        write(project_root, "library/notes.txt", "héllo\n")

        content = read_file_resource(workspace, project, "library/notes.txt")

        assert content.text == "héllo\n"
        assert content.blob is None
        assert content.mime_type == "text/plain"

    def test_resource_never_truncates(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        payload = b"z" * (2 * 1024 * 1024)
        write(project_root, "library/big.bin", payload)

        content = read_file_resource(workspace, project, "library/big.bin")

        assert content.blob == payload

    def test_declared_text_that_is_not_utf8_serves_exact_bytes(
        self, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        payload = b"header \xff\xfe tail"
        write(project_root, "library/weird.txt", payload)

        content = read_file_resource(workspace, project, "library/weird.txt")

        assert content.blob == payload
        assert content.text is None
