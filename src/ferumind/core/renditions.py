"""Bounded raster renditions for model context (spec-mcp §5.4).

``read_file`` never puts an original photograph into a tool result: a 5 MB
JPEG is far larger than a portable web-host tool result. It returns a
*rendition* — a re-encoded, size-bounded copy generated deterministically by
the server. The original is never modified and stays reachable byte-for-byte
through its ``ferumind://`` resource URI.

Renditions are mechanical: resize, re-encode, strip metadata. Nothing here
interprets image content.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

from ferumind.core.errors import (
    FileTooLargeError,
    RenditionTooLargeError,
    ValidationError,
)
from ferumind.core.file_io import read_regular_file_bytes
from ferumind.core.types import StrictModel

#: Longest-edge bounds for a context rendition. 1024 px retains useful
#: photographic comparison detail while avoiding the very large base64 tool
#: results produced by camera originals. The caller's edge and quality are
#: preferences: the encoded-byte ceiling below always takes precedence.
MIN_IMAGE_EDGE: Final = 256
MAX_IMAGE_EDGE: Final = 4096
DEFAULT_IMAGE_EDGE: Final = 1024

#: Lossy-encode quality bounds. Below ~40 artifacts start destroying the
#: detail the rendition exists to convey.
MIN_IMAGE_QUALITY: Final = 40
MAX_IMAGE_QUALITY: Final = 95
DEFAULT_IMAGE_QUALITY: Final = 78

#: Claude.ai documents an approximately 150,000-character MCP tool-result
#: ceiling, while ChatGPT does not publish a corresponding raw MCP result
#: limit. A 96 KiB JPEG expands to exactly 128 KiB of base64 *before* the
#: summary, structured metadata, and resource link are added; a live ChatGPT
#: result at that size did not surface its image block. Keep the binary at
#: 64 KiB so the complete MCP result stays comfortably below that observed
#: boundary. This is a generated rendition limit, not an original resource
#: limit.
MAX_IMAGE_RENDITION_BYTES: Final = 64 * 1024

#: A rendition exists to *bound* an original, so it must never be larger
#: than the original it stands in for. Re-encoding can inflate: a source
#: already saved at low quality, or squeezed by a better PNG optimizer than
#: Pillow's, comes back bigger at the same geometry. The per-file ceiling is
#: therefore the original's own size, not just the constant above.
#:
#: Below this floor the rule is dropped, because it stops meaning anything:
#: JPEG and PNG containers cost a fixed few hundred bytes whatever the
#: pixels, so a 200-byte icon *cannot* be re-encoded smaller, and refusing to
#: render it would trade a real capability for a saving of a few kilobytes.
#: The floor caps how much a rendition may ever exceed its original.
MIN_RENDITION_BYTE_CEILING: Final = 8 * 1024

#: Prefer spatial downscaling to severe JPEG artifacts. An explicit caller
#: request below this value is still honored; otherwise adaptive fitting
#: keeps at least this quality before reducing geometry.
_MIN_ADAPTIVE_JPEG_QUALITY: Final = 70

#: The public minimum is a request constraint, not a promise that a
#: pathologically incompressible transparent image can defeat the hard byte
#: ceiling. Adaptive shrinking may go this low in that exceptional case.
_MIN_ADAPTIVE_IMAGE_EDGE: Final = 128

#: Decompression-bomb ceiling, checked against the header-declared
#: dimensions *before* any pixel data is decoded. 100 megapixels is well
#: above any real camera output and far below what exhausts memory.
MAX_DECODED_PIXELS: Final = 100_000_000

JPEG_MIME_TYPE: Final = "image/jpeg"
PNG_MIME_TYPE: Final = "image/png"

#: Modes whose alpha channel or palette transparency would be destroyed by
#: a JPEG re-encode, forcing PNG output instead.
_TRANSPARENT_MODES: Final[frozenset[str]] = frozenset({"RGBA", "LA", "PA", "La"})


class ImageRendition(StrictModel):
    """A re-encoded, bounded copy of an image plus the original's geometry."""

    data: bytes
    mime_type: str
    width: int
    height: int
    original_width: int
    original_height: int
    resized: bool
    size_limited: bool
    encode_quality: int | None

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def clamp_edge(max_edge: int) -> int:
    """Validate a caller-supplied longest-edge bound."""
    if max_edge < MIN_IMAGE_EDGE or max_edge > MAX_IMAGE_EDGE:
        raise ValidationError(
            f"max_image_edge must be between {MIN_IMAGE_EDGE} and {MAX_IMAGE_EDGE}",
            details={"min": MIN_IMAGE_EDGE, "max": MAX_IMAGE_EDGE},
        )
    return max_edge


def clamp_quality(quality: int) -> int:
    """Validate a caller-supplied lossy-encode quality."""
    if quality < MIN_IMAGE_QUALITY or quality > MAX_IMAGE_QUALITY:
        raise ValidationError(
            f"image_quality must be between {MIN_IMAGE_QUALITY} and {MAX_IMAGE_QUALITY}",
            details={"min": MIN_IMAGE_QUALITY, "max": MAX_IMAGE_QUALITY},
        )
    return quality


def _needs_alpha(image: Image.Image) -> bool:
    if image.mode in _TRANSPARENT_MODES:
        return True
    return image.mode == "P" and "transparency" in image.info


def _encode_jpeg(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _encode_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def rendition_ceiling(original_size_bytes: int) -> int:
    """The effective encoded-byte ceiling for one source file.

    Never above :data:`MAX_IMAGE_RENDITION_BYTES`, never above the original's
    own size, and never below :data:`MIN_RENDITION_BYTE_CEILING` — see those
    constants for why each bound exists.
    """
    return max(MIN_RENDITION_BYTE_CEILING, min(MAX_IMAGE_RENDITION_BYTES, original_size_bytes))


def _next_edge(current_edge: int, encoded_bytes: int, ceiling: int) -> int:
    """Choose a smaller edge using encoded area as a conservative guide."""
    size_ratio = (ceiling / encoded_bytes) ** 0.5
    candidate = int(current_edge * min(0.85, size_ratio * 0.95))
    return max(_MIN_ADAPTIVE_IMAGE_EDGE, min(current_edge - 1, candidate))


def _best_jpeg_at_current_size(
    image: Image.Image, requested_quality: int, ceiling: int
) -> tuple[bytes, int]:
    """Return the highest-quality JPEG that fits, or the minimum-quality attempt."""
    requested = _encode_jpeg(image, requested_quality)
    if len(requested) <= ceiling:
        return requested, requested_quality

    quality_floor = min(requested_quality, _MIN_ADAPTIVE_JPEG_QUALITY)
    minimum = _encode_jpeg(image, quality_floor)
    if len(minimum) > ceiling:
        return minimum, quality_floor

    best_data = minimum
    best_quality = quality_floor
    low = quality_floor + 1
    high = requested_quality - 1
    while low <= high:
        candidate_quality = (low + high) // 2
        candidate = _encode_jpeg(image, candidate_quality)
        if len(candidate) <= ceiling:
            best_data = candidate
            best_quality = candidate_quality
            low = candidate_quality + 1
        else:
            high = candidate_quality - 1
    return best_data, best_quality


def _encode_within_ceiling(
    image: Image.Image,
    ceiling: int,
    encode: Callable[[Image.Image], tuple[bytes, int | None]],
) -> tuple[bytes, int | None, bool]:
    """Encode *image*, stepping its geometry down until the payload fits *ceiling*.

    The PNG and JPEG paths differ only in how a single frame is encoded, so
    the shrink loop belongs to neither of them.

    The third element reports whether the *first* encode overflowed, which is
    what makes a rendition ``size_limited``: a later pass fitting is the
    adaptation working, not the absence of one.
    """
    data, quality = encode(image)
    overflowed = len(data) > ceiling
    while len(data) > ceiling:
        current_edge = max(image.size)
        if current_edge <= _MIN_ADAPTIVE_IMAGE_EDGE:
            raise RenditionTooLargeError(
                "Image rendition could not fit the context byte limit",
                details={"limit_bytes": ceiling},
            )
        next_edge = _next_edge(current_edge, len(data), ceiling)
        image.thumbnail((next_edge, next_edge), Image.Resampling.LANCZOS)
        data, quality = encode(image)
    return data, quality, overflowed


def render_image_context(
    source: Path,
    *,
    max_edge: int = DEFAULT_IMAGE_EDGE,
    quality: int = DEFAULT_IMAGE_QUALITY,
) -> ImageRendition:
    """Read *source* and render it — see :func:`render_image_bytes`.

    A convenience wrapper for callers holding a path they trust. Anything
    serving a project file should read the bytes once through
    ``file_io.read_regular_file_bytes`` and call :func:`render_image_bytes`,
    so the digest and the rendition are provably derived from the same read
    rather than from two opens of the same name (S-06).
    """
    return render_image_bytes(
        read_regular_file_bytes(source),
        max_edge=max_edge,
        quality=quality,
    )


def render_image_bytes(
    data: bytes,
    *,
    max_edge: int = DEFAULT_IMAGE_EDGE,
    quality: int = DEFAULT_IMAGE_QUALITY,
) -> ImageRendition:
    """Produce a bounded rendition of *data* for model context.

    Applies EXIF orientation, preserves aspect ratio, and never upscales.
    Images carrying transparency are re-encoded as PNG so the alpha channel
    survives; everything else becomes JPEG. All metadata (EXIF, ICC, XMP) is
    dropped — the rendition is a viewing copy, not a replacement original.

    The encoded result is held under :func:`rendition_ceiling` for this
    source, so a rendition is never larger than the original it bounds.

    A file that is not a decodable raster image raises
    :class:`ValidationError`; an image whose declared geometry exceeds
    :data:`MAX_DECODED_PIXELS` raises :class:`FileTooLargeError`; a source
    that cannot be encoded under its ceiling at any supported geometry
    raises :class:`RenditionTooLargeError`. None of these escape as an
    unhandled Pillow exception.
    """
    edge = clamp_edge(max_edge)
    encode_quality = clamp_quality(quality)
    ceiling = rendition_ceiling(len(data))

    try:
        # Decoded from the bytes already in hand rather than reopened by name,
        # so the rendition cannot describe a different file than the one the
        # caller read and hashed. Derived images below are closed explicitly.
        with Image.open(io.BytesIO(data)) as image:
            # Dimensions come from the header, so this rejects a
            # decompression bomb before any pixel data is decoded.
            if image.width * image.height > MAX_DECODED_PIXELS:
                raise FileTooLargeError(
                    "Image exceeds the decodable pixel limit",
                    details={
                        "max_pixels": MAX_DECODED_PIXELS,
                        "width": image.width,
                        "height": image.height,
                    },
                )
            oriented = ImageOps.exif_transpose(image) or image
            try:
                # EXIF orientation may swap the axes; report the geometry a
                # viewer would actually see, not the stored one.
                upright_width, upright_height = oriented.size
                use_png = _needs_alpha(oriented)
                # ``thumbnail`` scales down only, never up, and preserves
                # the aspect ratio.
                working = oriented.copy()
            finally:
                if oriented is not image:
                    oriented.close()

        try:
            working.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            target_mode = "RGBA" if use_png else "RGB"
            encoded = working if working.mode == target_mode else working.convert(target_mode)
            try:
                if use_png:
                    payload, actual_quality, overflowed = _encode_within_ceiling(
                        encoded,
                        ceiling,
                        lambda image: (_encode_png(image), None),
                    )
                    size_limited = overflowed
                    mime_type = PNG_MIME_TYPE
                else:
                    payload, actual_quality, overflowed = _encode_within_ceiling(
                        encoded,
                        ceiling,
                        lambda image: _best_jpeg_at_current_size(image, encode_quality, ceiling),
                    )
                    # Equivalent to testing the first encode's quality: when
                    # the loop ran at all, `overflowed` is already true.
                    size_limited = overflowed or actual_quality != encode_quality
                    mime_type = JPEG_MIME_TYPE
                width, height = encoded.size
            finally:
                if encoded is not working:
                    encoded.close()
        finally:
            working.close()
    except FileTooLargeError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValidationError(
            "File could not be decoded as a supported raster image",
            details={"reason": type(exc).__name__},
        ) from exc

    return ImageRendition(
        data=payload,
        mime_type=mime_type,
        width=width,
        height=height,
        original_width=upright_width,
        original_height=upright_height,
        resized=(width, height) != (upright_width, upright_height),
        size_limited=size_limited,
        encode_quality=actual_quality,
    )
