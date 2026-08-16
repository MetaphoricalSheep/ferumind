"""Bounded image renditions: geometry, encoding, and failure modes."""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest
from PIL import Image

from ferumind.core.errors import (
    FileTooLargeError,
    RenditionTooLargeError,
    ValidationError,
)
from ferumind.core.renditions import (
    DEFAULT_IMAGE_EDGE,
    MAX_IMAGE_EDGE,
    MAX_IMAGE_QUALITY,
    MAX_IMAGE_RENDITION_BYTES,
    MIN_IMAGE_EDGE,
    MIN_IMAGE_QUALITY,
    MIN_RENDITION_BYTE_CEILING,
    render_image_context,
    rendition_ceiling,
)
from tests.conftest import png_declaring_geometry


def noisy_image(width: int, height: int, *, seed: int = 1234) -> Image.Image:
    """Build a deterministic high-entropy image.

    Noise matters: a flat colour compresses to a few kilobytes, so a
    meaningfully-sized fixture is impossible without it. This is the
    *pessimal* case for JPEG — see :func:`photograph_like` for the
    realistic one.
    """
    # Deterministic, test-only fixture data; not a security context.
    rng = random.Random(seed)  # noqa: S311
    return Image.frombytes("RGB", (width, height), rng.randbytes(width * height * 3))


class TestGeometry:
    def test_downscales_to_the_default_edge_preserving_aspect(self, tmp_path: Path) -> None:
        source = tmp_path / "wide.jpg"
        noisy_image(4000, 2000).save(source, format="JPEG", quality=80)

        # Isolate the requested geometry from the independent encoded-byte
        # ceiling: high-entropy noise at the default quality may need further
        # adaptive shrinking to fit the portable result budget.
        rendition = render_image_context(source, quality=MIN_IMAGE_QUALITY)

        assert max(rendition.width, rendition.height) == DEFAULT_IMAGE_EDGE
        assert rendition.width == 1024
        assert rendition.height == 512
        assert rendition.original_width == 4000
        assert rendition.original_height == 2000
        assert rendition.resized is True

    def test_aspect_ratio_is_preserved_within_a_pixel(self, tmp_path: Path) -> None:
        source = tmp_path / "odd.jpg"
        noisy_image(3000, 1731).save(source, format="JPEG", quality=80)

        rendition = render_image_context(source, max_edge=1024)

        original_ratio = 3000 / 1731
        rendition_ratio = rendition.width / rendition.height
        assert abs(original_ratio - rendition_ratio) < 0.01

    def test_small_images_are_never_upscaled(self, tmp_path: Path) -> None:
        source = tmp_path / "small.png"
        noisy_image(120, 80).save(source, format="PNG")

        rendition = render_image_context(source, max_edge=MAX_IMAGE_EDGE)

        assert (rendition.width, rendition.height) == (120, 80)
        assert rendition.resized is False

    def test_custom_edge_is_honoured(self, tmp_path: Path) -> None:
        source = tmp_path / "photo.jpg"
        noisy_image(2000, 1000).save(source, format="JPEG", quality=80)

        rendition = render_image_context(source, max_edge=512)

        assert max(rendition.width, rendition.height) == 512


class TestEncoding:
    def test_opaque_photograph_becomes_jpeg(self, tmp_path: Path) -> None:
        source = tmp_path / "photo.jpg"
        noisy_image(800, 600).save(source, format="JPEG", quality=80)

        assert render_image_context(source).mime_type == "image/jpeg"

    def test_transparent_png_stays_png_with_alpha(self, tmp_path: Path) -> None:
        source = tmp_path / "logo.png"
        image = Image.new("RGBA", (600, 400), (255, 0, 0, 0))
        image.putpixel((10, 10), (0, 255, 0, 255))
        image.save(source, format="PNG")

        rendition = render_image_context(source, max_edge=256)

        assert rendition.mime_type == "image/png"
        with Image.open(__import__("io").BytesIO(rendition.data)) as decoded:
            assert decoded.mode in ("RGBA", "LA")
            assert decoded.getchannel("A").getextrema()[0] == 0

    def test_palette_transparency_is_preserved_as_png(self, tmp_path: Path) -> None:
        source = tmp_path / "paletted.png"
        image = Image.new("P", (100, 100))
        image.info["transparency"] = 0
        image.save(source, format="PNG", transparency=0)

        assert render_image_context(source).mime_type == "image/png"

    def test_webp_is_rendered(self, tmp_path: Path) -> None:
        source = tmp_path / "shot.webp"
        noisy_image(900, 700).save(source, format="WEBP", quality=80)

        rendition = render_image_context(source, max_edge=400)

        assert rendition.mime_type in ("image/jpeg", "image/png")
        assert max(rendition.width, rendition.height) == 400

    def test_rendition_metadata_is_stripped(self, tmp_path: Path) -> None:
        source = tmp_path / "tagged.jpg"
        image = noisy_image(600, 400)
        exif = Image.Exif()
        exif[271] = "SecretCameraMaker"
        image.save(source, format="JPEG", quality=80, exif=exif)
        assert b"SecretCameraMaker" in source.read_bytes()

        rendition = render_image_context(source, max_edge=300)

        assert b"SecretCameraMaker" not in rendition.data

    def test_lower_quality_produces_a_smaller_payload(self, tmp_path: Path) -> None:
        source = tmp_path / "photo.jpg"
        noisy_image(1200, 900).save(source, format="JPEG", quality=95)

        high = render_image_context(source, max_edge=1024, quality=MAX_IMAGE_QUALITY)
        low = render_image_context(source, max_edge=1024, quality=MIN_IMAGE_QUALITY)

        assert low.size_bytes < high.size_bytes


class TestExifOrientation:
    def test_orientation_is_applied_and_axes_reported_upright(self, tmp_path: Path) -> None:
        source = tmp_path / "rotated.jpg"
        image = noisy_image(400, 200)
        exif = image.getexif()
        exif[274] = 6  # rotate 90° CW on display
        image.save(source, format="JPEG", quality=85, exif=exif)

        rendition = render_image_context(source, max_edge=MAX_IMAGE_EDGE)

        # Stored 400x200; a viewer honouring orientation sees 200x400.
        assert (rendition.original_width, rendition.original_height) == (200, 400)
        assert (rendition.width, rendition.height) == (200, 400)


class TestBounds:
    @pytest.mark.parametrize("edge", [MIN_IMAGE_EDGE - 1, MAX_IMAGE_EDGE + 1, 0, -5])
    def test_edge_bounds_are_enforced(self, tmp_path: Path, edge: int) -> None:
        source = tmp_path / "photo.jpg"
        noisy_image(100, 100).save(source, format="JPEG")
        with pytest.raises(ValidationError):
            render_image_context(source, max_edge=edge)

    @pytest.mark.parametrize("quality", [MIN_IMAGE_QUALITY - 1, MAX_IMAGE_QUALITY + 1, 0, 200])
    def test_quality_bounds_are_enforced(self, tmp_path: Path, quality: int) -> None:
        source = tmp_path / "photo.jpg"
        noisy_image(100, 100).save(source, format="JPEG")
        with pytest.raises(ValidationError):
            render_image_context(source, quality=quality)

    def test_decompression_bomb_is_refused_before_decoding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ferumind.core import renditions

        monkeypatch.setattr(renditions, "MAX_DECODED_PIXELS", 100)
        source = tmp_path / "big.png"
        noisy_image(64, 64).save(source, format="PNG")

        with pytest.raises(FileTooLargeError) as excinfo:
            render_image_context(source)
        assert excinfo.value.details is not None
        assert excinfo.value.details["max_pixels"] == 100

    def test_geometry_past_pillows_own_ceiling_is_still_a_size_refusal(
        self, tmp_path: Path
    ) -> None:
        """Pillow refuses inside ``Image.open``, before the explicit check.

        ``read_file`` puts no byte cap in front of this, so the guard is the
        only bound on a file that reached the workspace out of band. A size
        refusal must stay ``FILE_TOO_LARGE`` rather than escaping as an
        unhandled exception or being mislabelled as an undecodable file.
        """
        from ferumind.core import renditions

        pillow_ceiling = Image.MAX_IMAGE_PIXELS
        assert pillow_ceiling is not None
        edge = math.isqrt(2 * pillow_ceiling) + 1
        assert edge * edge > renditions.MAX_DECODED_PIXELS

        source = tmp_path / "bomb.png"
        source.write_bytes(png_declaring_geometry(edge, edge))

        with pytest.raises(FileTooLargeError) as excinfo:
            render_image_context(source)

        assert excinfo.value.code == "FILE_TOO_LARGE"
        assert excinfo.value.details is not None
        assert excinfo.value.details["max_pixels"] == renditions.MAX_DECODED_PIXELS

    def test_maximum_request_cannot_exceed_the_encoded_byte_ceiling(self, tmp_path: Path) -> None:
        source = tmp_path / "dense.jpg"
        noisy_image(3000, 2000).save(source, format="JPEG", quality=95)

        rendition = render_image_context(
            source,
            max_edge=MAX_IMAGE_EDGE,
            quality=MAX_IMAGE_QUALITY,
        )

        assert rendition.size_bytes <= MAX_IMAGE_RENDITION_BYTES
        assert rendition.size_limited is True

    def test_transparent_noise_is_shrunk_until_its_png_fits(self, tmp_path: Path) -> None:
        source = tmp_path / "dense-alpha.png"
        rgb = noisy_image(700, 700)
        alpha = Image.new("L", rgb.size, 127)
        rgba = rgb.convert("RGBA")
        rgba.putalpha(alpha)
        rgba.save(source, format="PNG")

        rendition = render_image_context(source, max_edge=700)

        assert rendition.mime_type == "image/png"
        assert rendition.size_bytes <= MAX_IMAGE_RENDITION_BYTES
        assert rendition.size_limited is True


class TestNeverLargerThanTheOriginal:
    """A rendition that inflates defeats the purpose of bounding anything.

    Observed live on 2026-08-04 (REL-030 defect 2): a 4,217,605-byte original
    came back as a 4,635,704-byte rendition, +9.9%. Tightening
    ``MAX_IMAGE_RENDITION_BYTES`` to 64 KiB shrank the blast radius but did
    not close the hole — a source already saved below the cap can still
    re-encode larger, which is what these tests pin.
    """

    def test_low_quality_source_does_not_re_encode_larger(self, tmp_path: Path) -> None:
        # Quality 20 is well under the default encode quality of 78, so a
        # naive re-encode at the same geometry inflates. Measured at 46,061
        # in, 61,669 out (1.34x) before the per-file ceiling landed.
        source = tmp_path / "already-squeezed.jpg"
        noisy_image(512, 512).save(source, format="JPEG", quality=20, optimize=True)
        original_size = source.stat().st_size
        assert original_size < MAX_IMAGE_RENDITION_BYTES, "fixture must sit under the global cap"

        rendition = render_image_context(source)

        assert rendition.size_bytes <= original_size
        assert rendition.size_bytes <= MAX_IMAGE_RENDITION_BYTES

    def test_ceiling_is_the_tighter_of_the_cap_and_the_original(self) -> None:
        assert rendition_ceiling(4_217_605) == MAX_IMAGE_RENDITION_BYTES
        assert rendition_ceiling(46_061) == 46_061
        # Container overhead dominates below the floor, so the rule lifts
        # rather than making small images unrenderable.
        assert rendition_ceiling(200) == MIN_RENDITION_BYTE_CEILING
        assert rendition_ceiling(0) == MIN_RENDITION_BYTE_CEILING

    def test_unfittable_source_raises_the_narrow_rendition_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ferumind.core import renditions

        # Drive both bounds to zero so no geometry can satisfy the ceiling.
        monkeypatch.setattr(renditions, "MIN_RENDITION_BYTE_CEILING", 1)
        monkeypatch.setattr(renditions, "MAX_IMAGE_RENDITION_BYTES", 1)
        source = tmp_path / "photo.jpg"
        noisy_image(900, 700).save(source, format="JPEG", quality=90)

        with pytest.raises(RenditionTooLargeError) as excinfo:
            render_image_context(source)
        # Still FILE_TOO_LARGE on the wire: this is a narrowing, not a new code.
        assert excinfo.value.code == "FILE_TOO_LARGE"
        assert isinstance(excinfo.value, FileTooLargeError)


class TestFailureModes:
    def test_non_image_bytes_fail_as_a_ferumind_error(self, tmp_path: Path) -> None:
        source = tmp_path / "not-an-image.jpg"
        source.write_bytes(b"this is plainly not a JPEG")

        with pytest.raises(ValidationError):
            render_image_context(source)

    def test_truncated_image_fails_cleanly(self, tmp_path: Path) -> None:
        source = tmp_path / "truncated.png"
        full = tmp_path / "full.png"
        noisy_image(400, 400).save(full, format="PNG")
        source.write_bytes(full.read_bytes()[:200])

        with pytest.raises(ValidationError):
            render_image_context(source)

    def test_empty_file_fails_cleanly(self, tmp_path: Path) -> None:
        source = tmp_path / "empty.jpg"
        source.write_bytes(b"")

        with pytest.raises(ValidationError):
            render_image_context(source)


class TestLargePhotograph:
    def test_five_megabyte_photo_renders_far_smaller(
        self, tmp_path: Path, large_photo_bytes: bytes
    ) -> None:
        source = tmp_path / "IMG_0042.jpg"
        source.write_bytes(large_photo_bytes)
        original_size = source.stat().st_size
        original_bytes = source.read_bytes()
        assert 4_000_000 < original_size < 7_000_000

        rendition = render_image_context(source)

        # An order of magnitude, not a rounding error: this is the whole
        # reason renditions exist rather than inlining the original.
        assert rendition.size_bytes <= MAX_IMAGE_RENDITION_BYTES
        assert max(rendition.width, rendition.height) <= DEFAULT_IMAGE_EDGE
        # The original is a viewing source, never a working copy.
        assert source.read_bytes() == original_bytes
        assert source.stat().st_size == original_size

    def test_a_smaller_edge_shrinks_the_payload_further(
        self, tmp_path: Path, large_photo_bytes: bytes
    ) -> None:
        """The caller's lever when a host rejects the default rendition."""
        source = tmp_path / "IMG_0042.jpg"
        source.write_bytes(large_photo_bytes)

        default = render_image_context(source)
        smaller = render_image_context(source, max_edge=768)

        assert smaller.size_bytes < default.size_bytes
        assert max(smaller.width, smaller.height) == 768
