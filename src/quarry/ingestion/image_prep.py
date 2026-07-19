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
        from PIL import Image  # noqa: PLC0415

        max_bytes = self._max_bytes
        name = self._image_path.name

        buf = io.BytesIO()
        img.save(buf, format=out_fmt, **save_kw)
        data = buf.getvalue()

        if max_bytes <= 0 or len(data) <= max_bytes:
            return data

        # Re-encode as JPEG if not already (much smaller for photos).
        if out_fmt != "JPEG":
            out_fmt, save_kw = "JPEG", {"quality": 95}
            rgb = img.convert("RGB") if img.mode != "RGB" else img
            buf = io.BytesIO()
            rgb.save(buf, format=out_fmt, **save_kw)
            data = buf.getvalue()
            logger.info("Re-encoded %s as JPEG (%d bytes)", name, len(data))
            if len(data) <= max_bytes:
                return data
            img = rgb

        # Downscale until under limit.
        current = img
        for _ in range(5):
            w, h = current.size
            new_w, new_h = max(1, w // 2), max(1, h // 2)
            if (new_w, new_h) == (w, h):
                break
            current = current.resize((new_w, new_h), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            current.save(buf, format=out_fmt, **save_kw)
            data = buf.getvalue()
            logger.info(
                "Downscaled %s to %dx%d (%d bytes)", name, new_w, new_h, len(data)
            )
            if len(data) <= max_bytes:
                return data

        if max_bytes > 0 and len(data) > max_bytes:
            logger.warning(
                "%s still %d bytes after downscaling (limit %d)",
                name,
                len(data),
                max_bytes,
            )

        return data
