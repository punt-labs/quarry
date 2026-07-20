"""Image analysis: detect format, page count, and OCR conversion needs.

OCR extraction itself lives in the ingestion pipeline, which reads this analysis
and prepares image bytes via ``ImagePreparer``; this module only inspects a file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SINGLE_PAGE_FORMATS = frozenset({".png", ".jpg", ".jpeg"})
MULTI_PAGE_FORMATS = frozenset({".tif", ".tiff"})
CONVERTIBLE_FORMATS = frozenset({".bmp", ".webp"})
SUPPORTED_IMAGE_EXTENSIONS = (
    SINGLE_PAGE_FORMATS | MULTI_PAGE_FORMATS | CONVERTIBLE_FORMATS
)

# Mapping from PIL format names to formats accepted by common OCR engines.
_PIL_TO_OCR: dict[str, str] = {
    "PNG": "PNG",
    "JPEG": "JPEG",
    "TIFF": "TIFF",
    "BMP": "BMP",
    "WEBP": "WEBP",
    "MPO": "JPEG",
}


@dataclass(frozen=True, slots=True)
class ImageAnalysis:
    """Result of analyzing a standalone image file."""

    format: str
    page_count: int
    needs_conversion: bool


class ImageExtractor:
    """Analyze standalone image files: format, page count, conversion needs."""

    @staticmethod
    def analyze(image_path: Path) -> ImageAnalysis:
        """Detect format, page count, and conversion requirements."""
        from PIL import Image  # noqa: PLC0415

        if not image_path.exists():
            msg = f"File not found: {image_path}"
            raise FileNotFoundError(msg)

        with Image.open(image_path) as im:
            pil_format = im.format
            if pil_format is None or pil_format not in _PIL_TO_OCR:
                msg = f"Unsupported image format: {pil_format}"
                raise ValueError(msg)

            # MPO frames are alternate camera shots, not document pages
            page_count = 1 if pil_format == "MPO" else getattr(im, "n_frames", 1)
            needs_conversion = (
                image_path.suffix.lower() in CONVERTIBLE_FORMATS or pil_format == "MPO"
            )

            logger.debug(
                "Image %s: format=%s, pages=%d, needs_conversion=%s",
                image_path.name,
                pil_format,
                page_count,
                needs_conversion,
            )

            return ImageAnalysis(
                format=pil_format,
                page_count=page_count,
                needs_conversion=needs_conversion,
            )
