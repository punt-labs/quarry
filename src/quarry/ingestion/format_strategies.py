"""Per-suffix extraction strategies: PDF, image, and text-like formats."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

# quarry-w1jr: quarry.extractors' package init eagerly imports code_extractor,
# which imports quarry.ingestion.text_splitter, which forces quarry.ingestion's
# own __init__ to run (Python always initializes a package's __init__ before any
# submodule) -- which reaches this module via pipeline.py.  If quarry.extractors
# is touched here BEFORE the text_format import below needs
# quarry.extractors.code_extractor a SECOND time, the first touch has already
# finished to completion and the re-entrant import resolves cleanly.  Keep the
# extractors imports textually before the text_format import -- swapping the
# order reproduces "ImportError: cannot import name 'SUPPORTED_CODE_EXTENSIONS'
# from partially initialized module" whenever quarry.extractors is the first
# quarry package touched in the process (see the pipeline-decomposition design,
# quarry-hb9u §d, for the full import-graph proof).
from quarry.extractors.image_extractor import SUPPORTED_IMAGE_EXTENSIONS, ImageExtractor
from quarry.extractors.pdf_extractor import PdfExtractor
from quarry.ingestion.backends import get_ocr_backend
from quarry.ingestion.extracted_document import ExtractedDocument, FormatStrategy
from quarry.ingestion.image_prep import ImagePreparer
from quarry.ingestion.ingest_stats import IngestStats
from quarry.ingestion.text_format import TEXT_LIKE_FORMATS, TextLikeFormat
from quarry.models import PageType

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.config import Settings
    from quarry.ingestion.ingest_context import Progress
    from quarry.types import OcrBackend

SUPPORTED_EXTENSIONS = (
    frozenset({".pdf"}) | SUPPORTED_IMAGE_EXTENSIONS | frozenset(TEXT_LIKE_FORMATS)
)


@final
@dataclass(frozen=True, slots=True)
class PdfFormatStrategy:
    """Extracts a PDF's pages, OCR-ing image pages via the injected backend."""

    _ocr: OcrBackend

    def extract(
        self,
        settings: Settings,
        file_path: Path,
        document_name: str,
        progress: Progress,
    ) -> ExtractedDocument:
        progress("Analyzing: %s", document_name)
        extractor = PdfExtractor(settings, self._ocr)
        all_pages = extractor.extract_pages(file_path, document_name=document_name)
        total_pages = len(all_pages)
        text_pages = sum(1 for p in all_pages if p.page_type == PageType.TEXT)
        image_pages = sum(1 for p in all_pages if p.page_type == PageType.IMAGE)
        progress(
            "Pages: %d total, %d text, %d image", total_pages, text_pages, image_pages
        )
        return ExtractedDocument(
            pages=all_pages,
            stats=IngestStats(
                total_pages=total_pages, text_pages=text_pages, image_pages=image_pages
            ),
            source_format=".pdf",
        )


@final
@dataclass(frozen=True, slots=True)
class ImageFormatStrategy:
    """Extracts a standalone image's pages, OCR-ing via the injected backend.

    Single-page images use the OCR backend's sync API; multi-page TIFFs use the
    async API (S3 for cloud backends, local for on-device backends).
    """

    _ocr: OcrBackend

    def extract(
        self,
        _settings: Settings,
        file_path: Path,
        document_name: str,
        progress: Progress,
    ) -> ExtractedDocument:
        progress("Analyzing image: %s", document_name)
        analysis = ImageExtractor.analyze(file_path)
        progress(
            "Image: %s, %d pages, conversion=%s",
            analysis.format,
            analysis.page_count,
            analysis.needs_conversion,
        )
        if analysis.page_count > 1:
            return self._extract_multipage(
                file_path, document_name, analysis.page_count, progress
            )
        image_bytes = ImagePreparer(
            file_path, needs_conversion=analysis.needs_conversion
        ).to_bytes()
        page = self._ocr.ocr_image_bytes(
            image_bytes, document_name=document_name, document_path=file_path.resolve()
        )
        return ExtractedDocument(
            pages=[page],
            stats=IngestStats(file_format=analysis.format, image_pages=1),
            source_format=file_path.suffix.lower(),
        )

    def _extract_multipage(
        self,
        file_path: Path,
        document_name: str,
        page_count: int,
        progress: Progress,
    ) -> ExtractedDocument:
        """Extract a multi-page image (TIFF) via the OCR backend's async path."""
        progress("Running OCR on %d pages (async)", page_count)
        all_page_numbers = list(range(1, page_count + 1))
        pages = self._ocr.ocr_document(
            file_path, all_page_numbers, page_count, document_name=document_name
        )
        return ExtractedDocument(
            pages=pages,
            stats=IngestStats(file_format="TIFF", image_pages=page_count),
            source_format=file_path.suffix.lower(),
        )


@final
@dataclass(frozen=True, slots=True)
class TextLikeStrategy:
    """Adapts a :class:`TextLikeFormat` (text_format.py, unchanged) to a strategy."""

    _fmt: TextLikeFormat

    def extract(
        self,
        settings: Settings,
        file_path: Path,
        document_name: str,
        progress: Progress,
    ) -> ExtractedDocument:
        progress("%s: %s", self._fmt.read_verb, document_name)
        pages = self._fmt.extract(settings, file_path, document_name)
        progress("%s: %d", self._fmt.unit_label, len(pages))
        return ExtractedDocument(
            pages=pages,
            stats=self._fmt.stats(len(pages)),
            source_format=file_path.suffix.lower(),
        )


def resolve_strategy(suffix: str, settings: Settings) -> FormatStrategy:
    """Return the extraction strategy for *suffix*, resolving OCR only if needed.

    Raises:
        ValueError: If *suffix* is not a supported format.
    """
    if suffix == ".pdf":
        return PdfFormatStrategy(get_ocr_backend(settings))
    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        return ImageFormatStrategy(get_ocr_backend(settings))
    fmt = TEXT_LIKE_FORMATS.get(suffix)
    if fmt is not None:
        return TextLikeStrategy(fmt)
    msg = f"Unsupported file format: {suffix}"
    raise ValueError(msg)
