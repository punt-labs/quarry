"""Image format extraction: analyze, OCR, and produce PageContent."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.ingestion.image_preparer import ImagePreparer
    from quarry.types import OcrBackend

from quarry.models import PageContent

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
    """Extract pages from image files via OCR.

    Implements ``FormatExtractor`` protocol. Composes ``ImagePreparer``
    for byte preparation and delegates OCR to the configured backend.
    """

    _preparer: ImagePreparer
    _ocr: OcrBackend
    _settings: Settings

    def __new__(
        cls,
        settings: Settings,
        ocr: OcrBackend,
        preparer: ImagePreparer,
    ) -> Self:
        self = super().__new__(cls)
        self._settings = settings
        self._ocr = ocr
        self._preparer = preparer
        return self

    def extract_pages(
        self,
        path: Path,
        *,
        document_name: str | None = None,
    ) -> list[PageContent]:
        """Extract pages from an image file via OCR."""
        document_name = document_name or path.name
        analysis = self.analyze(path)

        if analysis.page_count > 1:
            return self._extract_multipage(
                path,
                analysis.page_count,
                document_name=document_name,
            )

        image_bytes = self._preparer.prepare_bytes(
            path,
            needs_conversion=analysis.needs_conversion,
        )
        page = self._ocr.ocr_image_bytes(
            image_bytes,
            document_name=document_name,
            document_path=path.resolve(),
        )
        return [page]

    def _extract_multipage(
        self,
        file_path: Path,
        page_count: int,
        *,
        document_name: str,
    ) -> list[PageContent]:
        """Extract pages from a multi-page image (TIFF) via OCR."""
        all_page_numbers = list(range(1, page_count + 1))
        return self._ocr.ocr_document(
            file_path,
            all_page_numbers,
            page_count,
            document_name=document_name,
        )

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
