"""Storage-time image normalization.

Distinct from :mod:`ferumind.core.renditions`, which builds small throwaway
copies *for model context* and leaves the stored file untouched. This module
decides what actually lands on disk: uploads are downscaled and re-encoded
before they are written, so the workspace never accumulates camera-sized
originals that no transport can carry.

Three rules keep the pass safe to run over live data, repeatedly:

* **Format-preserving.** JPEG stays JPEG, PNG stays PNG, WebP stays WebP.
  Transcoding a screenshot or a chart to JPEG would soften the text those
  images exist to convey, so it never happens implicitly.
* **Never worse.** If re-encoding does not actually shrink the file, the
  original bytes are kept. A pass over already-normalized content is a no-op.
* **Never upscaling.** ``max_edge`` is a ceiling, not a target.
* **Never newly lossy.** A losslessly-encoded source (PNG, or WebP in VP8L
  form) is re-encoded losslessly. Squeezing a lossless screenshot or an OCR
  fixture through a lossy encoder destroys exactly the detail it was kept
  for, and no byte saving justifies that.

The pass is therefore idempotent: re-running it after changing the policy
converges on the new policy without compounding generation loss on files that
were already at or below it.
"""

from __future__ import annotations

import io
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

from ferumind.core.errors import FileTooLargeError, ValidationError
from ferumind.core.types import StrictModel

#: Storage-policy bounds. Wider than the rendition bounds because this is the
#: retained copy: 512 px is the smallest edge that keeps a photograph useful
#: for comparison, and 8192 px is above any consumer camera output.
MIN_STORAGE_EDGE: Final = 512
MAX_STORAGE_EDGE: Final = 8192

#: Quality floor of 60 is where JPEG artifacts begin to be visible on skin
#: and gradients; 100 is allowed so a policy can opt out of lossy loss.
MIN_STORAGE_QUALITY: Final = 60
MAX_STORAGE_QUALITY: Final = 100

#: Decompression-bomb ceiling, checked against header-declared dimensions
#: before any pixel data is decoded. Mirrors ``renditions.MAX_DECODED_PIXELS``.
MAX_DECODED_PIXELS: Final = 100_000_000

JPEG_MIME_TYPE: Final = "image/jpeg"
PNG_MIME_TYPE: Final = "image/png"
WEBP_MIME_TYPE: Final = "image/webp"

#: Pillow format name -> (mime type, is_lossy). Anything outside this map is
#: passed through untouched rather than guessed at: animated GIFs, multi-page
#: TIFFs and exotic formats all lose data under a naive single-frame
#: re-encode, and none of them are worth that risk here.
_SUPPORTED_FORMATS: Final[dict[str, tuple[str, bool]]] = {
    "JPEG": (JPEG_MIME_TYPE, True),
    "PNG": (PNG_MIME_TYPE, False),
    "WEBP": (WEBP_MIME_TYPE, True),
}

#: Modes whose transparency a JPEG re-encode would destroy.
_TRANSPARENT_MODES: Final[frozenset[str]] = frozenset({"RGBA", "LA", "PA", "La"})

#: Minimum proportional saving that justifies re-encoding an already-conforming
#: lossy image. Every lossy re-encode loses a little detail, and a JPEG
#: re-encoded at its own quality comes out slightly *smaller* each time — so a
#: naive "smaller is better" rule would quietly degrade a photograph a bit more
#: on every pass. Requiring a real saving makes repeated runs converge instead
#: of compounding generation loss. Resizes and lossless re-encodes are exempt:
#: the first is a genuine change, the second costs no fidelity at all.
MIN_LOSSY_REENCODE_GAIN: Final = 0.10

#: Why a file was left byte-identical. Surfaced in results and the CLI report
#: so a no-op is always explainable rather than silently indistinguishable
#: from a failure.
SKIP_DISABLED: Final = "compression_disabled"
SKIP_NOT_AN_IMAGE: Final = "not_a_decodable_image"
SKIP_UNSUPPORTED_FORMAT: Final = "unsupported_image_format"
SKIP_ANIMATED: Final = "animated_image"
SKIP_NO_GAIN: Final = "already_optimal"
SKIP_MARGINAL_GAIN: Final = "already_within_policy"


class ImagePolicy(StrictModel):
    """The storage policy applied to an uploaded or migrated raster."""

    max_edge: int = 2560
    jpeg_quality: int = 85
    enabled: bool = True

    def validated(self) -> ImagePolicy:
        """Reject an out-of-range policy before it touches any file."""
        if not MIN_STORAGE_EDGE <= self.max_edge <= MAX_STORAGE_EDGE:
            raise ValidationError(
                f"image max_edge must be between {MIN_STORAGE_EDGE} and {MAX_STORAGE_EDGE}",
                details={"min": MIN_STORAGE_EDGE, "max": MAX_STORAGE_EDGE},
            )
        if not MIN_STORAGE_QUALITY <= self.jpeg_quality <= MAX_STORAGE_QUALITY:
            raise ValidationError(
                f"image quality must be between {MIN_STORAGE_QUALITY} and {MAX_STORAGE_QUALITY}",
                details={"min": MIN_STORAGE_QUALITY, "max": MAX_STORAGE_QUALITY},
            )
        return self


class ImageCompressionResult(StrictModel):
    """Outcome of one normalization attempt.

    ``data`` is always the bytes that should be stored — the re-encoded copy
    when ``changed`` is true, and the caller's original bytes otherwise — so
    callers never branch to decide what to write.
    """

    data: bytes
    mime_type: str | None
    changed: bool
    reason: str | None
    original_size_bytes: int
    original_width: int | None = None
    original_height: int | None = None
    width: int | None = None
    height: int | None = None

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    @property
    def saved_bytes(self) -> int:
        return self.original_size_bytes - len(self.data)


def webp_is_lossless(raw: bytes) -> bool:
    """Report whether a WebP payload uses lossless (VP8L) coding.

    A bare ``VP8L`` chunk is lossless and a bare ``VP8 `` chunk is lossy; the
    extended ``VP8X`` container has to be walked to find whichever it holds.
    Anything unparseable is reported as lossless, because the only use of this
    answer is deciding whether a lossy re-encode is permissible — and guessing
    "lossy" there would destroy data.
    """
    if len(raw) < 16 or raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
        return True
    fourcc = raw[12:16]
    if fourcc == b"VP8L":
        return True
    if fourcc == b"VP8 ":
        return False
    if fourcc != b"VP8X":
        return True
    # Extended container: walk chunk headers looking for the image data.
    offset = 12
    while offset + 8 <= len(raw):
        chunk = raw[offset : offset + 4]
        try:
            size = int.from_bytes(raw[offset + 4 : offset + 8], "little")
        except ValueError:  # pragma: no cover - slice is always 4 bytes
            return True
        if chunk == b"VP8L":
            return True
        if chunk == b"VP8 ":
            return False
        # Chunks are padded to an even length.
        offset += 8 + size + (size & 1)
    return True


def _encode(
    image: Image.Image,
    pillow_format: str,
    quality: int,
    exif: bytes | None,
    *,
    lossless: bool,
) -> bytes:
    """Re-encode *image*, carrying EXIF through for the formats that hold it."""
    buffer = io.BytesIO()
    params: dict[str, object] = {"optimize": True}
    if pillow_format == "JPEG":
        params["quality"] = quality
        params["progressive"] = True
    elif pillow_format == "WEBP":
        if lossless:
            params["lossless"] = True
            # For lossless WebP this selects compression effort, not fidelity.
            params["quality"] = 100
        else:
            params["quality"] = quality
    if exif:
        params["exif"] = exif
    image.save(buffer, format=pillow_format, **params)  # pyright: ignore[reportArgumentType]
    return buffer.getvalue()


def _unchanged(raw: bytes, mime_type: str | None, reason: str) -> ImageCompressionResult:
    return ImageCompressionResult(
        data=raw,
        mime_type=mime_type,
        changed=False,
        reason=reason,
        original_size_bytes=len(raw),
    )


def compress_image_for_storage(
    raw: bytes,
    *,
    mime_type: str | None = None,
    policy: ImagePolicy | None = None,
) -> ImageCompressionResult:
    """Normalize *raw* for storage under *policy*.

    Returns the bytes to store. Non-images, unsupported or animated formats,
    and files that would not actually shrink are returned untouched with a
    ``reason`` explaining why — this function never raises for "not worth
    compressing", only for genuinely hostile input.

    Raises :class:`FileTooLargeError` if the header-declared geometry exceeds
    :data:`MAX_DECODED_PIXELS`, so a decompression bomb is refused before any
    pixel data is decoded.
    """
    active = (policy or ImagePolicy()).validated()
    if not active.enabled:
        return _unchanged(raw, mime_type, SKIP_DISABLED)

    try:
        with Image.open(io.BytesIO(raw)) as probe:
            if probe.width * probe.height > MAX_DECODED_PIXELS:
                raise FileTooLargeError(
                    "Image exceeds the decodable pixel limit",
                    details={
                        "max_pixels": MAX_DECODED_PIXELS,
                        "width": probe.width,
                        "height": probe.height,
                    },
                )
            pillow_format = probe.format or ""
            if pillow_format not in _SUPPORTED_FORMATS:
                return _unchanged(raw, mime_type, SKIP_UNSUPPORTED_FORMAT)
            if getattr(probe, "n_frames", 1) > 1:
                # Re-encoding would silently flatten an animation to one frame.
                return _unchanged(raw, mime_type, SKIP_ANIMATED)

            resolved_mime, _is_lossy = _SUPPORTED_FORMATS[pillow_format]
            # PNG is always lossless; WebP depends on how it was authored.
            keep_lossless = pillow_format == "PNG" or (
                pillow_format == "WEBP" and webp_is_lossless(raw)
            )
            oriented = ImageOps.exif_transpose(probe) or probe
            try:
                original_width, original_height = oriented.size
                # ``exif_transpose`` normalizes the orientation tag, so the
                # carried EXIF (capture time, camera body) stays truthful.
                exif_bytes = oriented.info.get("exif") if pillow_format != "PNG" else None
                working = oriented.copy()
            finally:
                if oriented is not probe:
                    oriented.close()
    except FileTooLargeError:
        raise
    except (UnidentifiedImageError, OSError, ValueError):
        # Not a decodable raster: a PDF, a text file, a truncated download.
        # Storage normalization simply does not apply.
        return _unchanged(raw, mime_type, SKIP_NOT_AN_IMAGE)

    try:
        resized = max(working.size) > active.max_edge
        if resized:
            working.thumbnail((active.max_edge, active.max_edge), Image.Resampling.LANCZOS)

        if pillow_format == "JPEG" and working.mode not in {"RGB", "L", "CMYK"}:
            target = working.convert("RGB")
        elif pillow_format == "WEBP" and working.mode not in {"RGB", "RGBA"}:
            target = working.convert("RGBA" if working.mode in _TRANSPARENT_MODES else "RGB")
        else:
            target = working
        try:
            encoded = _encode(
                target,
                pillow_format,
                active.jpeg_quality,
                exif_bytes,
                lossless=keep_lossless,
            )
            width, height = target.size
        finally:
            if target is not working:
                target.close()
    finally:
        working.close()

    kept_mime = resolved_mime if mime_type is None else mime_type
    if len(encoded) >= len(raw):
        # Already at or below what this policy would produce. Keeping the
        # original also avoids a pointless extra lossy generation.
        return _unchanged(raw, kept_mime, SKIP_NO_GAIN)

    if not resized and not keep_lossless:
        gain = (len(raw) - len(encoded)) / len(raw)
        if gain < MIN_LOSSY_REENCODE_GAIN:
            # Within policy already: the small saving on offer is not worth
            # spending another lossy generation on the stored copy.
            return _unchanged(raw, kept_mime, SKIP_MARGINAL_GAIN)

    return ImageCompressionResult(
        data=encoded,
        mime_type=resolved_mime,
        changed=True,
        reason=None,
        original_size_bytes=len(raw),
        original_width=original_width,
        original_height=original_height,
        width=width,
        height=height,
    )
