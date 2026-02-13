"""Image format detection and conversion requirements for OCR."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

SINGLE_PAGE_FORMATS = frozenset({".png", ".jpg", ".jpeg"})
MULTI_PAGE_FORMATS = frozenset({".tif", ".tiff"})
CONVERTIBLE_FORMATS = frozenset({".bmp", ".webp"})
SUPPORTED_IMAGE_EXTENSIONS = (
    SINGLE_PAGE_FORMATS | MULTI_PAGE_FORMATS | CONVERTIBLE_FORMATS
)

# Mapping from PIL format names to formats accepted by common OCR engines.
# BMP/WebP require conversion to PNG/JPEG before OCR.
_PIL_TO_OCR: dict[str, str] = {
    "PNG": "PNG",
    "JPEG": "JPEG",
    "TIFF": "TIFF",
    "BMP": "BMP",
    "WEBP": "WEBP",
    "MPO": "JPEG",  # iPhone multi-picture; first frame is standard JPEG
}


@dataclass(frozen=True)
class ImageAnalysis:
    """Result of analyzing a standalone image file."""

    format: str  # PIL format name: "PNG", "JPEG", "TIFF", "BMP", "WEBP"
    page_count: int  # 1 for single-page, N for multi-page TIFF
    needs_conversion: bool  # True if must convert to PNG/JPEG for OCR backends


def analyze_image(image_path: Path) -> ImageAnalysis:
    """Analyze an image file: detect format and page count.

    needs_conversion is True for BMP/WebP — OCR backends typically
    require PNG or JPEG; these formats must be converted before OCR.

    Args:
        image_path: Path to image file.

    Returns:
        ImageAnalysis with format, page count, and conversion flag.

    Raises:
        FileNotFoundError: If image_path does not exist.
        ValueError: If image format is not supported.
    """
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
