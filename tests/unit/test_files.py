"""Generic project file discovery, classification, and sidecar recognition."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ferumind.core.errors import ValidationError
from ferumind.core.file_uri import parse_file_uri
from ferumind.core.files import (
    classify_context_support,
    is_recognized_upload_sidecar,
    list_project_files,
    resolve_mime_type,
    sidecar_for_path,
)
from ferumind.core.paths import PathSafetyError, WorkspaceRoot, contained_project_root
from ferumind.core.writes import upload_library_file


@pytest.fixture
def project_root(workspace: WorkspaceRoot, project: str) -> Path:
    return contained_project_root(workspace, project)


def write(root: Path, relative: str, content: bytes | str = b"x") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        target.write_text(content, encoding="utf-8")
    else:
        target.write_bytes(content)
    return target


def paths(root: Path, key: str, **kwargs: object) -> list[str]:
    listing = list_project_files(root, key, **kwargs)  # pyright: ignore[reportArgumentType]
    return [entry.path for entry in listing.files]


class TestClassification:
    @pytest.mark.parametrize(
        ("path", "mime", "support"),
        [
            ("a/b.jpg", "image/jpeg", "image"),
            ("a/b.JPEG", "image/jpeg", "image"),
            ("a/b.png", "image/png", "image"),
            ("a/b.webp", "image/webp", "image"),
            ("a/b.gif", "image/gif", "resource_only"),
            ("a/b.svg", "image/svg+xml", "text"),
            ("a/b.pdf", "application/pdf", "resource_only"),
            ("a/b.txt", "text/plain", "text"),
            ("a/b.md", "text/markdown", "text"),
            ("a/b.json", "application/json", "text"),
            ("a/b.xlsx", None, "resource_only"),
            ("a/b.unknownext", "application/octet-stream", "resource_only"),
            ("a/noextension", "application/octet-stream", "resource_only"),
        ],
    )
    def test_mime_and_context_support(self, path: str, mime: str | None, support: str) -> None:
        resolved = resolve_mime_type(path)
        if mime is not None:
            assert resolved == mime
        assert classify_context_support(resolved) == support

    def test_gif_stays_resource_only(self) -> None:
        # Documented choice: a first frame is not the animation, so GIF is
        # never presented as if the model saw the image.
        assert classify_context_support(resolve_mime_type("x.gif")) == "resource_only"


class TestDiscovery:
    def test_finds_files_in_library_and_arbitrary_nested_locations(
        self, project_root: Path, project: str
    ) -> None:
        write(project_root, "library/photo.jpg")
        write(project_root, "canvases/deep/nested/dir/export.csv")
        write(project_root, "inbox/scan.pdf")

        found = paths(project_root, project)
        assert found == [
            "canvases/deep/nested/dir/export.csv",
            "inbox/scan.pdf",
            "library/photo.jpg",
        ]

    def test_paths_with_spaces_and_unicode(self, project_root: Path, project: str) -> None:
        write(project_root, "library/trip photos/café föto.jpg")
        write(project_root, "library/日本語/写真.png")

        found = paths(project_root, project)
        assert found == ["library/trip photos/café föto.jpg", "library/日本語/写真.png"]

    def test_every_entry_carries_a_resolvable_resource_uri(
        self, project_root: Path, project: str
    ) -> None:
        write(project_root, "library/trip photos/café föto.jpg")
        entry = list_project_files(project_root, project).files[0]
        parsed = parse_file_uri(entry.resource_uri)
        assert parsed.project_key == project
        assert parsed.path == entry.path

    def test_no_absolute_path_is_leaked(self, project_root: Path, project: str) -> None:
        write(project_root, "library/photo.jpg")
        listing = list_project_files(project_root, project)
        serialized = listing.model_dump_json()
        assert str(project_root) not in serialized
        assert "/tmp" not in serialized

    def test_markdown_excluded_by_default_and_included_on_request(
        self, project_root: Path, project: str
    ) -> None:
        write(project_root, "canvases/plan.md", "# Plan\n")
        write(project_root, "library/photo.jpg")

        assert paths(project_root, project) == ["library/photo.jpg"]
        assert "canvases/plan.md" in paths(project_root, project, include_markdown=True)

    def test_directories_are_never_listed(self, project_root: Path, project: str) -> None:
        (project_root / "library" / "emptydir").mkdir(parents=True)
        write(project_root, "library/photo.jpg")
        assert paths(project_root, project) == ["library/photo.jpg"]

    def test_ferumind_internals_are_excluded(self, project_root: Path, project: str) -> None:
        write(project_root, ".ferumind/snapshots/secret.bin")
        write(project_root, "library/photo.jpg")
        assert paths(project_root, project) == ["library/photo.jpg"]

    def test_symlinked_file_and_directory_are_skipped(
        self, project_root: Path, project: str, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.jpg"
        outside.write_bytes(b"secret")
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "inner.jpg").write_bytes(b"secret")

        write(project_root, "library/real.jpg")
        (project_root / "library" / "link.jpg").symlink_to(outside)
        (project_root / "library" / "linkdir").symlink_to(outside_dir)

        assert paths(project_root, project) == ["library/real.jpg"]

    def test_path_prefix_filters_and_rejects_escape(self, project_root: Path, project: str) -> None:
        write(project_root, "library/a.jpg")
        write(project_root, "inbox/b.jpg")

        assert paths(project_root, project, path_prefix="library") == ["library/a.jpg"]
        with pytest.raises(PathSafetyError):
            list_project_files(project_root, project, path_prefix="../../etc")

    def test_path_prefix_does_not_match_a_sibling_directory(
        self, project_root: Path, project: str
    ) -> None:
        # The classic string-prefix bug: "library" must not select
        # "library-archive/". The filter normalizes to "library/".
        write(project_root, "library/a.jpg")
        write(project_root, "library-archive/b.jpg")

        assert paths(project_root, project, path_prefix="library") == ["library/a.jpg"]
        assert paths(project_root, project, path_prefix="library/") == ["library/a.jpg"]

    def test_mime_and_extension_filters(self, project_root: Path, project: str) -> None:
        write(project_root, "library/a.jpg")
        write(project_root, "library/b.png")
        write(project_root, "library/c.pdf")

        assert paths(project_root, project, mime_type="image/png") == ["library/b.png"]
        assert paths(project_root, project, extension=".pdf") == ["library/c.pdf"]
        assert paths(project_root, project, extension="jpg") == ["library/a.jpg"]

    def test_query_matches_filename_case_insensitively(
        self, project_root: Path, project: str
    ) -> None:
        write(project_root, "library/Front-View.JPG")
        write(project_root, "library/other.jpg")

        assert paths(project_root, project, query="front-view") == ["library/Front-View.JPG"]
        assert paths(project_root, project, query="FRONT") == ["library/Front-View.JPG"]

    def test_query_matches_path_mime_and_extension(self, project_root: Path, project: str) -> None:
        write(project_root, "library/receipts/a.pdf")
        write(project_root, "library/photo.jpg")

        assert paths(project_root, project, query="receipts") == ["library/receipts/a.pdf"]
        assert paths(project_root, project, query="image/jpeg") == ["library/photo.jpg"]
        assert paths(project_root, project, query=".pdf") == ["library/receipts/a.pdf"]


class TestPagination:
    def test_pagination_is_deterministic_and_complete(
        self, project_root: Path, project: str
    ) -> None:
        for index in range(7):
            write(project_root, f"library/file-{index}.bin")

        collected: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            listing = list_project_files(project_root, project, limit=3, cursor=cursor)
            collected.extend(entry.path for entry in listing.files)
            pages += 1
            if not listing.has_more:
                break
            cursor = listing.next_cursor
            assert cursor is not None
        assert pages == 3
        assert collected == sorted(collected)
        assert collected == [f"library/file-{index}.bin" for index in range(7)]

    def test_repeating_a_page_returns_the_same_rows(self, project_root: Path, project: str) -> None:
        for index in range(5):
            write(project_root, f"library/f{index}.bin")
        first = list_project_files(project_root, project, limit=2)
        again = list_project_files(project_root, project, limit=2)
        assert [item.path for item in first.files] == [item.path for item in again.files]

    def test_malformed_cursor_is_rejected(self, project_root: Path, project: str) -> None:
        with pytest.raises(ValidationError):
            list_project_files(project_root, project, cursor="!!!not-a-cursor!!!")

    def test_limit_bounds_are_enforced(self, project_root: Path, project: str) -> None:
        with pytest.raises(ValidationError):
            list_project_files(project_root, project, limit=0)
        with pytest.raises(ValidationError):
            list_project_files(project_root, project, limit=10_000)


class TestSidecars:
    @pytest.fixture
    def uploaded(self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str) -> str:
        import base64

        result = upload_library_file(
            conn,
            workspace,
            project,
            filename="photo.jpg",
            content_base64=base64.b64encode(b"not-a-real-jpeg").decode(),
            mime_type="image/jpeg",
            metadata={"caption": "front elevation", "revision": 3},
        )
        return result.path

    def test_generated_sidecar_is_hidden_by_default(
        self, project_root: Path, project: str, uploaded: str
    ) -> None:
        assert uploaded == "library/photo.jpg"
        assert paths(project_root, project) == ["library/photo.jpg"]

    @pytest.mark.usefixtures("uploaded")
    def test_generated_sidecar_is_listable_on_request(
        self, project_root: Path, project: str
    ) -> None:
        listed = paths(project_root, project, include_sidecars=True)
        assert listed == ["library/photo.jpg", "library/photo.json"]
        sidecar_entry = next(
            item
            for item in list_project_files(project_root, project, include_sidecars=True).files
            if item.path == "library/photo.json"
        )
        assert sidecar_entry.is_upload_sidecar is True

    @pytest.mark.usefixtures("uploaded")
    def test_content_entry_carries_bounded_sidecar_metadata(
        self, project_root: Path, project: str
    ) -> None:
        entry = list_project_files(project_root, project).files[0]
        assert entry.sidecar is not None
        assert entry.sidecar.path == "library/photo.json"
        assert entry.sidecar.metadata["caption"] == "front elevation"
        assert entry.sidecar.metadata["sha256"]

    @pytest.mark.usefixtures("uploaded")
    def test_query_matches_sidecar_scalar_metadata(self, project_root: Path, project: str) -> None:
        assert paths(project_root, project, query="front elevation") == ["library/photo.jpg"]
        assert paths(project_root, project, query="nothing-matches-this") == []

    def test_user_authored_json_sharing_a_stem_is_not_hidden(
        self, project_root: Path, project: str
    ) -> None:
        write(project_root, "library/data.csv", "a,b\n1,2\n")
        write(project_root, "library/data.json", '{"mine": true}')

        listed = paths(project_root, project)
        assert "library/data.json" in listed
        assert is_recognized_upload_sidecar(project_root, "library/data.json") is None

    def test_json_without_a_content_sibling_is_a_normal_file(
        self, project_root: Path, project: str
    ) -> None:
        write(project_root, "library/standalone.json", '{"anything": 1}')
        assert paths(project_root, project) == ["library/standalone.json"]

    def test_malformed_sidecar_is_ignored_as_metadata_but_still_listed(
        self, project_root: Path, project: str
    ) -> None:
        write(project_root, "library/photo.jpg")
        write(project_root, "library/photo.json", "{ this is not json")

        listing = list_project_files(project_root, project)
        listed = [item.path for item in listing.files]
        assert listed == ["library/photo.jpg", "library/photo.json"]
        assert listing.files[0].sidecar is None
        assert sidecar_for_path(project_root, "library/photo.jpg") is None

    def test_oversized_sidecar_is_not_parsed(self, project_root: Path) -> None:
        write(project_root, "library/photo.jpg")
        payload = {
            "original_filename": "photo.jpg",
            "uploaded_at": "now",
            "uploaded_by_tool": "test",
            "sha256": "0" * 64,
            "size_bytes": 1,
            "padding": "x" * (128 * 1024),
        }
        write(project_root, "library/photo.json", json.dumps(payload))
        assert sidecar_for_path(project_root, "library/photo.jpg") is None

    def test_sidecar_metadata_values_are_bounded(self, project_root: Path) -> None:
        write(project_root, "library/photo.jpg")
        payload = {
            "original_filename": "photo.jpg",
            "uploaded_at": "now",
            "uploaded_by_tool": "test",
            "sha256": "0" * 64,
            "size_bytes": 1,
            "note": "y" * 5_000,
            "nested": {"dropped": True},
        }
        write(project_root, "library/photo.json", json.dumps(payload))
        sidecar = sidecar_for_path(project_root, "library/photo.jpg")
        assert sidecar is not None
        note = sidecar.metadata["note"]
        assert isinstance(note, str)
        assert len(note) <= 120
        assert "nested" not in sidecar.metadata

    def test_files_without_sidecars_remain_discoverable(
        self, project_root: Path, project: str
    ) -> None:
        write(project_root, "library/dropped-in-by-hand.jpg")
        entry = list_project_files(project_root, project).files[0]
        assert entry.path == "library/dropped-in-by-hand.jpg"
        assert entry.sidecar is None
