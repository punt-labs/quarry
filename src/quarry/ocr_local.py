"""OCR backend implementation using RapidOCR (local, no AWS)."""

from __future__ import annotations

import io
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, runtime_checkable

import fitz
from PIL import Image

from quarry.config import Settings
from quarry.models import PageContent, PageType

logger = logging.getLogger(__name__)


class _OcrResult(Protocol):
    """Structural type for RapidOCR v3 output."""

    @property
    def txts(self) -> tuple[str, ...] | None: ...


@runtime_checkable
class _OcrEngine(Protocol):
    """Structural type for RapidOCR engine."""

    def __call__(self, img: Image.Image) -> _OcrResult: ...


_engine: _OcrEngine | None = None
_engine_lock = threading.Lock()


def _get_engine() -> _OcrEngine:
    """Return a cached RapidOCR engine instance.

    Thread-safe via double-checked locking. The engine is initialized
    once per process. ONNX models are bundled in the rapidocr package
    (~17 MB) and loaded on first call.
    """
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from rapidocr import RapidOCR  # noqa: PLC0415

                _engine = RapidOCR()
                logger.info("RapidOCR engine initialized")
    return _engine


def _extract_text(result: _OcrResult) -> str:
    """Extract text lines from a RapidOCR output.

    RapidOCR v3 returns an object with a ``txts`` attribute that is
    either a tuple of strings (text detected) or ``None`` (no text).
    """
    if result.txts is None:
        return ""
    return "\n".join(str(t) for t in result.txts)


def _render_pdf_page(doc: fitz.Document, page_number: int) -> Image.Image:
    """Render a 1-indexed PDF page to a PIL Image at 200 DPI."""
    page = doc[page_number - 1]
    pix = page.get_pixmap(dpi=200)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _ocr_pages(
    pages: Iterator[tuple[int, Image.Image]],
    document_name: str,
    document_path: str,
    total_pages: int,
) -> list[PageContent]:
    """OCR a sequence of (page_number, image) pairs."""
    engine = _get_engine()
    results: list[PageContent] = []
    for page_num, img in pages:
        text = _extract_text(engine(img))
        logger.info(
            "OCR page %d/%d of %s: %d chars",
            page_num,
            total_pages,
            document_name,
            len(text),
        )
        results.append(
            PageContent(
                document_name=document_name,
                document_path=document_path,
                page_number=page_num,
                total_pages=total_pages,
                text=text,
                page_type=PageType.IMAGE,
            )
        )
    return results


class LocalOcrBackend:
    """OCR backend using RapidOCR (offline ONNX models).

    Satisfies the ``OcrBackend`` protocol. No cloud credentials required.
    Uses PaddleOCR models via ONNX Runtime for CPU-only inference.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def ocr_document(
        self,
        document_path: Path,
        page_numbers: list[int],
        total_pages: int,
        *,
        document_name: str | None = None,
    ) -> list[PageContent]:
        """OCR pages from a document (PDF or TIFF)."""
        doc_name = document_name or document_path.name
        doc_path = str(document_path.resolve())
        suffix = document_path.suffix.lower()

        if suffix in (".tif", ".tiff"):
            return self._ocr_tiff(
                document_path, page_numbers, total_pages, doc_name, doc_path
            )
        if suffix == ".pdf":
            return self._ocr_pdf(
                document_path, page_numbers, total_pages, doc_name, doc_path
            )
        msg = f"Unsupported document type for OCR: '{suffix}'"
        raise ValueError(msg)

    def ocr_image_bytes(
        self,
        image_bytes: bytes,
        document_name: str,
        document_path: Path,
    ) -> PageContent:
        """OCR a single-page image from bytes."""
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        text = _extract_text(_get_engine()(img))
        logger.info("OCR image %s: %d chars", document_name, len(text))
        return PageContent(
            document_name=document_name,
            document_path=str(document_path),
            page_number=1,
            total_pages=1,
            text=text,
            page_type=PageType.IMAGE,
        )

    @staticmethod
    def _ocr_pdf(
        pdf_path: Path,
        page_numbers: list[int],
        total_pages: int,
        document_name: str,
        document_path: str,
    ) -> list[PageContent]:
        with fitz.open(pdf_path) as doc:
            pages = ((num, _render_pdf_page(doc, num)) for num in page_numbers)
            return _ocr_pages(pages, document_name, document_path, total_pages)

    @staticmethod
    def _ocr_tiff(
        tiff_path: Path,
        page_numbers: list[int],
        total_pages: int,
        document_name: str,
        document_path: str,
    ) -> list[PageContent]:
        def frames() -> Iterator[tuple[int, Image.Image]]:
            with Image.open(tiff_path) as im:
                for page_num in page_numbers:
                    im.seek(page_num - 1)
                    yield page_num, im.copy().convert("RGB")

        return _ocr_pages(frames(), document_name, document_path, total_pages)
