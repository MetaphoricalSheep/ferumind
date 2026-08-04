"""Canonical ``lattice://file`` URI encoding and parsing."""

from __future__ import annotations

import base64

import pytest

from lattice.core.errors import ValidationError
from lattice.core.file_uri import (
    FILE_URI_PREFIX,
    MAX_ENCODED_PATH_CHARS,
    build_file_uri,
    decode_relative_path,
    encode_relative_path,
    parse_file_uri,
)

ROUND_TRIP_PATHS = [
    "library/photo.jpg",
    "library/trip photos/front view.jpg",
    "library/café/föto ✓.jpg",
    "canvases/nested/deeply/set/notes.txt",
    "library/odd+name&chars=1.pdf",
    "library/100% done (final).png",
    "library/日本語/写真.jpeg",
]


class TestRoundTrip:
    @pytest.mark.parametrize("path", ROUND_TRIP_PATHS)
    def test_paths_round_trip_exactly(self, path: str) -> None:
        parsed = parse_file_uri(build_file_uri("demo", path))
        assert parsed.project_key == "demo"
        assert parsed.path == path

    def test_uri_has_the_documented_shape(self) -> None:
        uri = build_file_uri("demo", "library/photo.jpg")
        assert uri.startswith(f"{FILE_URI_PREFIX}demo/")
        assert uri == f"{FILE_URI_PREFIX}demo/{encode_relative_path('library/photo.jpg')}"

    def test_uri_never_contains_a_filesystem_path(self) -> None:
        uri = build_file_uri("demo", "library/photo.jpg")
        assert "/home/" not in uri
        assert "workspace" not in uri
        assert "library/photo.jpg" not in uri

    def test_encoding_is_unpadded_base64url(self) -> None:
        encoded = encode_relative_path("library/a?b+c/d.jpg")
        assert "=" not in encoded
        assert "+" not in encoded
        assert "/" not in encoded


class TestRejection:
    @pytest.mark.parametrize(
        "uri",
        [
            "file:///etc/passwd",
            "https://example.com/x",
            "lattice://other/demo/aGk",
            "lattice://file/demo",
            "lattice://file/demo/aGk/extra",
            "lattice://file/demo/",
            "lattice://file/Demo/aGk",
            "lattice://file/1bad/aGk",
            "lattice://file/demo/not base64",
            "lattice://file/demo/@@@",
        ],
    )
    def test_malformed_uris_are_rejected(self, uri: str) -> None:
        with pytest.raises(ValidationError):
            parse_file_uri(uri)

    def test_padded_encoding_is_not_canonical(self) -> None:
        padded = base64.urlsafe_b64encode(b"library/photo.jpg").decode()
        assert padded.endswith("=")
        with pytest.raises(ValidationError):
            parse_file_uri(f"{FILE_URI_PREFIX}demo/{padded}")

    def test_standard_alphabet_encoding_is_rejected(self) -> None:
        # A path whose standard-base64 form uses '+' or '/'.
        raw = b"library/\xfb\xef\xbe.jpg"
        standard = base64.b64encode(raw).decode().rstrip("=")
        assert "+" in standard or "/" in standard
        with pytest.raises(ValidationError):
            parse_file_uri(f"{FILE_URI_PREFIX}demo/{standard}")

    def test_non_canonical_trailing_bits_are_rejected(self) -> None:
        canonical = encode_relative_path("ab")
        mutated = canonical[:-1] + ("B" if canonical[-1] != "B" else "C")
        assert mutated != canonical
        with pytest.raises(ValidationError):
            decode_relative_path(mutated)

    def test_non_utf8_payload_is_rejected(self) -> None:
        encoded = base64.urlsafe_b64encode(b"\xff\xfe\xfd").decode().rstrip("=")
        with pytest.raises(ValidationError):
            decode_relative_path(encoded)

    def test_oversized_segment_is_rejected_before_decoding(self) -> None:
        with pytest.raises(ValidationError):
            decode_relative_path("A" * (MAX_ENCODED_PATH_CHARS + 4))

    def test_empty_segment_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            decode_relative_path("")

    def test_traversal_path_decodes_but_is_not_blessed(self) -> None:
        # The URI layer decodes it; containment is enforced downstream, and
        # that separation is deliberate — assert the decode is faithful.
        uri = build_file_uri("demo", "../../etc/passwd")
        assert parse_file_uri(uri).path == "../../etc/passwd"
