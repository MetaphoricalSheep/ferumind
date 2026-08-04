"""Storage image normalization (``lattice.core.images``)."""

from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image

from lattice.core.errors import FileTooLargeError, ValidationError
from lattice.core.images import (
    MIN_LOSSY_REENCODE_GAIN,
    SKIP_ANIMATED,
    SKIP_DISABLED,
    SKIP_MARGINAL_GAIN,
    SKIP_NO_GAIN,
    SKIP_NOT_AN_IMAGE,
    SKIP_UNSUPPORTED_FORMAT,
    ImagePolicy,
    compress_image_for_storage,
    webp_is_lossless,
)
from tests.conftest import photograph_like


def _pixels(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        return hashlib.sha256(image.convert("RGBA").tobytes()).digest()


def _png(width: int = 400, height: int = 300) -> bytes:
    buffer = io.BytesIO()
    photograph_like(width, height).save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def _webp(*, lossless: bool, width: int = 400, height: int = 300) -> bytes:
    buffer = io.BytesIO()
    photograph_like(width, height).save(
        buffer, format="WEBP", lossless=lossless, quality=100 if lossless else 90
    )
    return buffer.getvalue()


class TestPolicyValidation:
    def test_rejects_out_of_range_edge(self) -> None:
        with pytest.raises(ValidationError):
            ImagePolicy(max_edge=64).validated()

    def test_rejects_out_of_range_quality(self) -> None:
        with pytest.raises(ValidationError):
            ImagePolicy(jpeg_quality=5).validated()

    def test_accepts_bounds(self) -> None:
        assert ImagePolicy(max_edge=512, jpeg_quality=60).validated().max_edge == 512


class TestJpegCompression:
    def test_large_photo_shrinks_and_is_bounded(self, large_photo_bytes: bytes) -> None:
        result = compress_image_for_storage(
            large_photo_bytes, policy=ImagePolicy(max_edge=2560, jpeg_quality=85)
        )
        assert result.changed
        assert result.size_bytes < len(large_photo_bytes)
        assert max(result.width or 0, result.height or 0) == 2560

    def test_is_idempotent(self, large_photo_bytes: bytes) -> None:
        policy = ImagePolicy(max_edge=2560, jpeg_quality=85)
        first = compress_image_for_storage(large_photo_bytes, policy=policy)
        second = compress_image_for_storage(first.data, policy=policy)
        assert first.changed
        assert not second.changed
        assert second.reason == SKIP_NO_GAIN
        assert second.data == first.data

    def test_repeated_passes_do_not_accumulate_generation_loss(self) -> None:
        """Regression: re-encoding a JPEG at its own quality shrinks it slightly.

        A naive "smaller is better" rule therefore rewrites the file on every
        pass, degrading the photograph a little each time. Convergence must be
        reached and held.
        """
        policy = ImagePolicy(max_edge=1024, jpeg_quality=85)
        buffer = io.BytesIO()
        photograph_like(3000, 2000).save(buffer, format="JPEG", quality=95)

        current = compress_image_for_storage(buffer.getvalue(), policy=policy).data
        for _ in range(5):
            following = compress_image_for_storage(current, policy=policy)
            assert not following.changed
            assert following.data == current
            current = following.data

    def test_marginal_lossy_gain_is_declined(self) -> None:
        policy = ImagePolicy(max_edge=4096, jpeg_quality=85)
        buffer = io.BytesIO()
        photograph_like(900, 700).save(buffer, format="JPEG", quality=86, optimize=True)
        source = buffer.getvalue()

        result = compress_image_for_storage(source, policy=policy)

        assert not result.changed
        assert result.reason in {SKIP_MARGINAL_GAIN, SKIP_NO_GAIN}
        assert result.data == source

    def test_a_real_saving_is_still_taken_without_a_resize(self) -> None:
        policy = ImagePolicy(max_edge=4096, jpeg_quality=60)
        buffer = io.BytesIO()
        photograph_like(1200, 900).save(buffer, format="JPEG", quality=98, optimize=False)
        source = buffer.getvalue()

        result = compress_image_for_storage(source, policy=policy)

        assert result.changed
        assert result.size_bytes < len(source) * (1 - MIN_LOSSY_REENCODE_GAIN)

    def test_never_upscales_a_small_image(self) -> None:
        buffer = io.BytesIO()
        photograph_like(320, 240).save(buffer, format="JPEG", quality=85)
        small = buffer.getvalue()
        result = compress_image_for_storage(small, policy=ImagePolicy(max_edge=4096))
        assert (result.width or 320) <= 320

    def test_preserves_exif_capture_time(self) -> None:
        source = io.BytesIO()
        image = photograph_like(3000, 2000)
        exif = image.getexif()
        exif.get_ifd(0x8769)[0x9003] = "2026:07:20:12:44:09"
        image.save(source, format="JPEG", quality=95, exif=exif.tobytes())

        result = compress_image_for_storage(source.getvalue(), policy=ImagePolicy(max_edge=1024))

        assert result.changed
        with Image.open(io.BytesIO(result.data)) as stored:
            assert stored.getexif().get_ifd(0x8769).get(0x9003) == "2026:07:20:12:44:09"


class TestLosslessSourcesStayLossless:
    def test_png_is_pixel_identical(self) -> None:
        source = _png()
        result = compress_image_for_storage(source, policy=ImagePolicy(max_edge=4096))
        if result.changed:
            assert _pixels(result.data) == _pixels(source)
            with Image.open(io.BytesIO(result.data)) as stored:
                assert stored.format == "PNG"

    def test_lossless_webp_is_pixel_identical(self) -> None:
        source = _webp(lossless=True)
        assert webp_is_lossless(source)
        result = compress_image_for_storage(source, policy=ImagePolicy(max_edge=4096))
        if result.changed:
            assert _pixels(result.data) == _pixels(source)
            assert webp_is_lossless(result.data)

    def test_downscaled_png_stays_png_and_lossless(self) -> None:
        source = _png(2000, 1500)
        result = compress_image_for_storage(source, policy=ImagePolicy(max_edge=512))
        assert result.changed
        with Image.open(io.BytesIO(result.data)) as stored:
            assert stored.format == "PNG"
            assert max(stored.size) == 512

    def test_lossy_webp_is_not_reported_lossless(self) -> None:
        assert not webp_is_lossless(_webp(lossless=False))

    def test_unparseable_webp_defaults_to_lossless(self) -> None:
        # Guessing "lossy" here would authorize a destructive re-encode.
        assert webp_is_lossless(b"not a webp at all")


class TestPassThrough:
    def test_non_image_is_untouched(self) -> None:
        payload = b"%PDF-1.7 this is not a raster image"
        result = compress_image_for_storage(payload)
        assert not result.changed
        assert result.data == payload
        assert result.reason == SKIP_NOT_AN_IMAGE

    def test_disabled_policy_is_untouched(self, large_photo_bytes: bytes) -> None:
        result = compress_image_for_storage(large_photo_bytes, policy=ImagePolicy(enabled=False))
        assert not result.changed
        assert result.reason == SKIP_DISABLED
        assert result.data == large_photo_bytes

    def test_animated_gif_is_not_flattened(self) -> None:
        buffer = io.BytesIO()
        frames = [photograph_like(80, 60, seed=n).convert("P") for n in range(3)]
        frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:])
        source = buffer.getvalue()

        result = compress_image_for_storage(source, policy=ImagePolicy(max_edge=512))

        assert not result.changed
        assert result.data == source
        assert result.reason in {SKIP_ANIMATED, SKIP_UNSUPPORTED_FORMAT}

    def test_already_small_jpeg_is_kept(self) -> None:
        buffer = io.BytesIO()
        photograph_like(200, 150).save(buffer, format="JPEG", quality=40, optimize=True)
        source = buffer.getvalue()
        result = compress_image_for_storage(source, policy=ImagePolicy(jpeg_quality=95))
        assert not result.changed
        assert result.reason == SKIP_NO_GAIN
        assert result.data == source


class TestDecompressionBomb:
    def test_oversized_geometry_is_refused_before_decode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lattice.core import images

        monkeypatch.setattr(images, "MAX_DECODED_PIXELS", 1000)
        with pytest.raises(FileTooLargeError):
            compress_image_for_storage(_png(200, 200))
