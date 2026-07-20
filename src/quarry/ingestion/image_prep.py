"""Prepare an image for OCR: format conversion and byte-budget downscaling."""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from pathlib import Path

    from PIL.Image import Image as PILImage

logger = logging.getLogger(__name__)


@final
class ImagePreparer:
    """Read an image as OCR-ready bytes, converting format and downscaling to fit.

    Format conversion: MPO -> JPEG, BMP/WebP -> PNG, with EXIF transpose.
    Size reduction: when the byte budget is positive and the encoded image
    exceeds it, lossless formats are re-encoded as JPEG (quality 95); if still
    too large, dimensions are halved up to five times.
    """

    __slots__ = ("_image_path", "_max_bytes", "_needs_conversion")

    _image_path: Path
    _needs_conversion: bool
    _max_bytes: int

    def __new__(
        cls, image_path: Path, *, needs_conversion: bool, max_bytes: int = 0
    ) -> Self:
        self = super().__new__(cls)
        self._image_path = image_path
        self._needs_conversion = needs_conversion
        self._max_bytes = max_bytes
        return self

    def to_bytes(self) -> bytes:
        """Return the image as OCR-ready bytes, converting/downscaling as needed."""
        if not self._needs_conversion and self._max_bytes <= 0:
            return self._image_path.read_bytes()

        if not self._needs_conversion:
            raw = self._image_path.read_bytes()
            if len(raw) <= self._max_bytes:
                return raw

        from PIL import Image, ImageOps  # noqa: PLC0415

        with Image.open(self._image_path) as im:
            img = ImageOps.exif_transpose(im)

            out_fmt: str
            save_kw: dict[str, int]
            if im.format == "MPO":
                out_fmt, save_kw = "JPEG", {"quality": 95}
            elif im.format in ("BMP", "WEBP"):
                out_fmt, save_kw = "PNG", {}
            else:
                out_fmt, save_kw = im.format or "PNG", {}

            return self._encode_to_fit(img, out_fmt, save_kw)

    def _encode_to_fit(
        self, img: PILImage, out_fmt: str, save_kw: dict[str, int]
    ) -> bytes:
        """Encode *img*, re-encoding as JPEG and/or downscaling if oversized.

        Strategy: save as-is; if over limit, re-encode as JPEG (much smaller for
        photos); then downscale by halves until under limit.  Keeps quality while
        meeting OCR engine byte limits.
        """
        data = self._encode(img, out_fmt, save_kw)
        if self._fits(data):
            return data
        if out_fmt != "JPEG":
            img, out_fmt, save_kw, data = self._reencode_as_jpeg(img)
            if self._fits(data):
                return data
        return self._downscale_to_fit(img, out_fmt, save_kw, data)

    def _fits(self, data: bytes) -> bool:
        """Whether *data* is within the byte budget (a non-positive budget fits)."""
        return self._max_bytes <= 0 or len(data) <= self._max_bytes

    @staticmethod
    def _encode(img: PILImage, out_fmt: str, save_kw: dict[str, int]) -> bytes:
        """Return *img* encoded as *out_fmt* bytes."""
        buf = io.BytesIO()
        img.save(buf, format=out_fmt, **save_kw)
        return buf.getvalue()

    def _reencode_as_jpeg(
        self, img: PILImage
    ) -> tuple[PILImage, str, dict[str, int], bytes]:
        """Re-encode *img* as an RGB JPEG (much smaller for photos)."""
        rgb = img.convert("RGB") if img.mode != "RGB" else img
        save_kw = {"quality": 95}
        data = self._encode(rgb, "JPEG", save_kw)
        logger.info(
            "Re-encoded %s as JPEG (%d bytes)", self._image_path.name, len(data)
        )
        return rgb, "JPEG", save_kw, data

    def _downscale_to_fit(
        self, img: PILImage, out_fmt: str, save_kw: dict[str, int], data: bytes
    ) -> bytes:
        """Halve *img*'s dimensions up to five times until it fits the budget."""
        from PIL import Image  # noqa: PLC0415

        name = self._image_path.name
        current = img
        for _ in range(5):
            w, h = current.size
            new_w, new_h = max(1, w // 2), max(1, h // 2)
            if (new_w, new_h) == (w, h):
                break
            current = current.resize((new_w, new_h), Image.Resampling.LANCZOS)
            data = self._encode(current, out_fmt, save_kw)
            logger.info(
                "Downscaled %s to %dx%d (%d bytes)", name, new_w, new_h, len(data)
            )
            if self._fits(data):
                return data

        if not self._fits(data):
            logger.warning(
                "%s still %d bytes after downscaling (limit %d)",
                name,
                len(data),
                self._max_bytes,
            )
        return data
