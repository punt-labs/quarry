from __future__ import annotations

import io
import logging
from pathlib import Path

import fitz
from PIL import Image

from quarry.config import Settings
from quarry.models import PageContent, PageType

logger = logging.getLogger(__name__)

_engine: object | None = None


def _get_engine() -> object:
    """Return a cached RapidOCR engine instance.

    The engine is initialized once per process. ONNX models are bundled
    in the rapidocr package (~17 MB) and loaded on first call.
    """
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR  # noqa: PLC0415

        _engine = RapidOCR()
        logger.info("RapidOCR engine initialized")
    return _engine


def _extract_text(result: object) -> str:
    """Extract text lines from a RapidOCROutput.

    RapidOCR v3 returns an object with a `txts` attribute that is
    either a tuple of strings (text detected) or None (no text).
    """
    txts = getattr(result, "txts", None)
    if txts is None:
        return ""
    return "\n".join(str(t) for t in txts)


def _render_pdf_page(doc: fitz.Document, page_number: int) -> Image.Image:
    """Render a 1-indexed PDF page to a PIL Image at 200 DPI."""
    page = doc[page_number - 1]
    pix = page.get_pixmap(dpi=200)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def ocr_pdf_pages(
    pdf_path: Path,
    page_numbers: list[int],
    total_pages: int,
    document_name: str,
) -> list[PageContent]:
    """OCR specific pages of a PDF by rendering each to an image.

    Uses PyMuPDF to render pages at 200 DPI, then runs RapidOCR
    on each rendered image.

    Args:
        pdf_path: Path to the PDF file.
        page_numbers: 1-indexed page numbers to OCR.
        total_pages: Total pages in the document.
        document_name: Document name for metadata.

    Returns:
        List of PageContent for each requested page.
    """
    engine = _get_engine()
    results: list[PageContent] = []
    doc_path = str(pdf_path.resolve())

    with fitz.open(pdf_path) as doc:
        for page_num in page_numbers:
            img = _render_pdf_page(doc, page_num)
            ocr_result = engine(img)  # type: ignore[operator]
            text = _extract_text(ocr_result)
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
                    document_path=doc_path,
                    page_number=page_num,
                    total_pages=total_pages,
                    text=text,
                    page_type=PageType.IMAGE,
                )
            )

    return results


def ocr_tiff_pages(
    tiff_path: Path,
    page_numbers: list[int],
    total_pages: int,
    document_name: str,
) -> list[PageContent]:
    """OCR specific frames of a multi-page TIFF.

    Uses PIL to extract individual frames, then runs RapidOCR on each.

    Args:
        tiff_path: Path to the TIFF file.
        page_numbers: 1-indexed page numbers to OCR.
        total_pages: Total pages in the document.
        document_name: Document name for metadata.

    Returns:
        List of PageContent for each requested page.
    """
    engine = _get_engine()
    results: list[PageContent] = []
    doc_path = str(tiff_path.resolve())

    with Image.open(tiff_path) as im:
        for page_num in page_numbers:
            im.seek(page_num - 1)
            frame = im.copy().convert("RGB")
            ocr_result = engine(frame)  # type: ignore[operator]
            text = _extract_text(ocr_result)
            logger.info(
                "OCR TIFF frame %d/%d of %s: %d chars",
                page_num,
                total_pages,
                document_name,
                len(text),
            )
            results.append(
                PageContent(
                    document_name=document_name,
                    document_path=doc_path,
                    page_number=page_num,
                    total_pages=total_pages,
                    text=text,
                    page_type=PageType.IMAGE,
                )
            )

    return results


def ocr_image_from_bytes(
    image_bytes: bytes,
    document_name: str,
    document_path: str,
) -> PageContent:
    """OCR a single-page image from bytes.

    Args:
        image_bytes: Image file bytes (any PIL-supported format).
        document_name: Document name for metadata.
        document_path: Full path string for metadata.

    Returns:
        PageContent for the single page.
    """
    engine = _get_engine()
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    ocr_result = engine(img)  # type: ignore[operator]
    text = _extract_text(ocr_result)
    logger.info(
        "OCR image %s: %d chars",
        document_name,
        len(text),
    )
    return PageContent(
        document_name=document_name,
        document_path=document_path,
        page_number=1,
        total_pages=1,
        text=text,
        page_type=PageType.IMAGE,
    )


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
        """OCR multiple pages from a document (PDF or TIFF)."""
        doc_name = document_name or document_path.name
        suffix = document_path.suffix.lower()

        if suffix in (".tif", ".tiff"):
            return ocr_tiff_pages(document_path, page_numbers, total_pages, doc_name)
        return ocr_pdf_pages(document_path, page_numbers, total_pages, doc_name)

    def ocr_image_bytes(
        self,
        image_bytes: bytes,
        document_name: str,
        document_path: str,
    ) -> PageContent:
        """OCR a single-page image from bytes."""
        return ocr_image_from_bytes(image_bytes, document_name, document_path)
