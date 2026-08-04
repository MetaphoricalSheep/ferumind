"""Canonical ``lattice://`` file resource URIs (spec-mcp §5.4).

One shared helper builds and parses the URIs used by both the file tools
and ``resources/read``, so a URI minted by ``list_files`` is always
resolvable by the resource handler and vice versa.

Shape::

    lattice://file/<project-key>/<base64url-unpadded(project-relative path)>

The project-relative path is encoded rather than embedded literally so the
URI safely survives spaces, Unicode, nested directories, and punctuation
without a second escaping layer. Encoding is canonical: the unpadded
base64url form is the only accepted spelling, and any other variant
(padded, standard-alphabet, whitespace-bearing) is rejected rather than
normalized. That keeps one URI per file, which matters because clients
cache and deduplicate resources by URI string.

The URI never contains the server-local workspace path: a project key plus
a project-relative path is all a stateless resolve needs.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Final

from lattice.core.errors import ValidationError
from lattice.core.types import StrictModel

#: Scheme + authority. ``file`` is the resource *kind*, not a filesystem
#: reference — it distinguishes file resources from any later kind without
#: reusing the reserved ``file://`` scheme.
FILE_URI_PREFIX: Final = "lattice://file/"

#: Bounds the encoded segment so a hostile URI cannot force a large decode.
#: 4096 path bytes (``MAX_RELATIVE_PATH_BYTES``) encode to 5464 characters.
MAX_ENCODED_PATH_CHARS: Final = 6000

_BASE64URL_RE: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_PROJECT_KEY_RE: Final = re.compile(r"^[a-z][a-z0-9-]*$")


class ParsedFileUri(StrictModel):
    """The project scope and project-relative path carried by a file URI."""

    project_key: str
    path: str


def encode_relative_path(path: str) -> str:
    """Encode a project-relative path as canonical unpadded base64url."""
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")


def decode_relative_path(encoded: str) -> str:
    """Decode a canonical unpadded base64url path segment.

    Shared by URI parsing and listing cursors so both reject the same
    non-canonical spellings. Raises :class:`ValidationError` on anything
    that is not the exact encoding :func:`encode_relative_path` produces.
    """
    if not encoded or len(encoded) > MAX_ENCODED_PATH_CHARS:
        raise ValidationError(
            "Encoded path segment is empty or exceeds the encoded length limit",
            details={"max_encoded_chars": MAX_ENCODED_PATH_CHARS},
        )
    if _BASE64URL_RE.fullmatch(encoded) is None:
        raise ValidationError("Encoded path segment must be unpadded base64url (A-Z a-z 0-9 - _)")
    padding = "=" * (-len(encoded) % 4)
    try:
        raw = base64.urlsafe_b64decode(encoded + padding)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("Encoded path segment is not valid base64url") from exc
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("Encoded path segment does not decode to UTF-8") from exc
    if not path:
        raise ValidationError("Encoded path segment decodes to an empty path")
    # One canonical spelling per file: reject any encoding that would decode
    # to the same path but not round-trip (stray padding, alphabet mixing,
    # trailing bits set in the final sextet).
    if encode_relative_path(path) != encoded:
        raise ValidationError("Encoded path segment is not in its canonical form")
    return path


def build_file_uri(project_key: str, path: str) -> str:
    """Build the canonical ``lattice://file/...`` URI for a project file."""
    return f"{FILE_URI_PREFIX}{project_key}/{encode_relative_path(path)}"


def parse_file_uri(uri: str) -> ParsedFileUri:
    """Parse and validate a Lattice file URI.

    Rejects a foreign scheme, a missing or extra segment, a non-canonical
    encoding, and a project key that is not registry-shaped. Returns the
    decoded project-relative path *without* resolving it — containment is
    the caller's job, through ``contained_path``.
    """
    if not uri.startswith(FILE_URI_PREFIX):
        raise ValidationError(
            f"Not a Lattice file resource URI; expected a {FILE_URI_PREFIX!r} prefix",
            details={"expected_prefix": FILE_URI_PREFIX},
        )
    remainder = uri[len(FILE_URI_PREFIX) :]
    segments = remainder.split("/")
    if len(segments) != 2:
        raise ValidationError(
            "Lattice file URI must have exactly a project segment and an encoded path segment"
        )
    project_key, encoded = segments
    if _PROJECT_KEY_RE.fullmatch(project_key) is None:
        raise ValidationError("Lattice file URI carries a malformed project key")
    return ParsedFileUri(project_key=project_key, path=decode_relative_path(encoded))
