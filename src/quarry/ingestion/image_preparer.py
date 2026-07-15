"""Image byte preparation: format conversion and size reduction for OCR."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

logger = logging.getLogger(__name__)


class ImagePreparer:
    """Prepare image bytes for OCR backends.

    Stateless class with one public method. Exists as a class (not a
    bare function) so that ``ImageExtractor`` can compose an
    ``_preparer: ImagePreparer`` and tests can inject a mock.
    """

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def prepare_bytes(
        self,
        image_path: Path,
        *,
        needs_conversion: bool,
        max_bytes: int = 0,
    ) -> bytes:
        """Read image bytes, converting format and/or downscaling as needed.

        Format conversion: MPO -> JPEG, BMP/WebP -> PNG, with EXIF transpose.
        Size reduction: when *max_bytes* > 0 and the encoded image exceeds it,
        lossless formats are re-encoded as JPEG (quality 95); if still too large,
        dimensions are halved up to 5 times.
        """
        if not needs_conversion and max_bytes <= 0:
            return image_path.read_bytes()

        if not needs_conversion:
            raw = image_path.read_bytes()
            if len(raw) <= max_bytes:
                return raw

        from PIL import Image, ImageOps  # noqa: PLC0415

        with Image.open(image_path) as im:
            img = ImageOps.exif_transpose(im)

            out_fmt: str
            save_kw: dict[str, int]
            if im.format == "MPO":
                out_fmt, save_kw = "JPEG", {"quality": 95}
            elif im.format in ("BMP", "WEBP"):
                out_fmt, save_kw = "PNG", {}
            else:
                out_fmt, save_kw = im.format or "PNG", {}

            return self._encode_to_fit(
                img, out_fmt, save_kw, max_bytes, image_path.name
            )

    def _encode_to_fit(
        self,
        img: PILImage,
        out_fmt: str,
        save_kw: dict[str, int],
        max_bytes: int,
        name: str,
    ) -> bytes:
        """Encode image, re-encoding as JPEG and/or downscaling if oversized."""
        import io  # noqa: PLC0415

        from PIL import Image  # noqa: PLC0415

        buf = io.BytesIO()
        img.save(buf, format=out_fmt, **save_kw)
        data = buf.getvalue()

        if max_bytes <= 0 or len(data) <= max_bytes:
            return data

        # Re-encode as JPEG if not already (much smaller for photos)
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

        # Downscale until under limit
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
                "Downscaled %s to %dx%d (%d bytes)",
                name,
                new_w,
                new_h,
                len(data),
            )
            if len(data) <= max_bytes:
                return data

        if len(data) > max_bytes:  # max_bytes > 0 here — the <=0 case returned above
            logger.warning(
                "%s still %d bytes after downscaling (limit %d)",
                name,
                len(data),
                max_bytes,
            )

        return data
