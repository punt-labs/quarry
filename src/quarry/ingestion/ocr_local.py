"""OCR backend implementation using RapidOCR (local, no AWS)."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

import pymupdf
from PIL import Image

from quarry.config import Settings
from quarry.ingestion.ocr_engine import (
    OCR_UNAVAILABLE,
    OcrEngine,
    OcrResult,
)
from quarry.models import PageContent, PageType

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OcrJob:
    """The per-document context an OCR run threads through its page loop."""

    page_numbers: tuple[int, ...]
    total_pages: int
    document_name: str
    document_path: str
    suffix: str

    def __post_init__(self) -> None:
        """Reject document formats unsupported by the OCR backend."""
        if self.suffix not in (".tif", ".tiff", ".pdf"):
            msg = f"Unsupported document type for OCR: '{self.suffix}'"
            raise ValueError(msg)


class LocalOcrBackend:
    """OCR backend using RapidOCR (offline ONNX models).

    Satisfies the ``OcrBackend`` protocol. No cloud credentials required.
    Uses PaddleOCR models via ONNX Runtime for CPU-only inference.
    """

    _settings: Settings

    # Process-wide latch: OCR availability is global (the engine is a
    # process-wide singleton), so one warning covers a whole directory of
    # scans instead of one per page.
    _warned_unavailable: ClassVar[bool] = False

    def __new__(cls, settings: Settings) -> Self:
        """Bind OCR execution to settings without loading a model."""
        self = super().__new__(cls)
        self._settings = settings
        return self

    def ocr_document(
        self,
        document_path: Path,
        page_numbers: list[int],
        total_pages: int,
        *,
        document_name: str | None = None,
    ) -> list[PageContent]:
        """OCR pages from a document (PDF or TIFF).

        Degrade cleanly when OCR is unavailable (headless cv2 or missing
        rapidocr): warn once and return no pages so the caller keeps indexing
        the document's extractable text instead of crashing ingestion.
        """
        job = OcrJob(
            page_numbers=tuple(page_numbers),
            total_pages=total_pages,
            document_name=document_name or document_path.name,
            document_path=str(document_path.resolve()),
            suffix=document_path.suffix.lower(),
        )
        try:
            if job.suffix in (".tif", ".tiff"):
                return self._ocr_tiff(document_path, job)
            return self._ocr_pdf(document_path, job)
        except OCR_UNAVAILABLE as exc:
            self._warn_unavailable(exc)
            return []

    def ocr_image_bytes(
        self,
        image_bytes: bytes,
        document_name: str,
        document_path: Path,
    ) -> PageContent:
        """OCR a single-page image from bytes.

        Degrade to empty text (which chunks to nothing downstream) when OCR is
        unavailable, so a scanned image on a headless box indexes without OCR
        rather than crashing.
        """
        with Image.open(io.BytesIO(image_bytes)) as opened:
            img = opened.convert("RGB")
            try:
                text = self._extract_text(OcrEngine.get()(img))
            except OCR_UNAVAILABLE as exc:
                self._warn_unavailable(exc)
                text = ""
        logger.info("OCR image %s: %d chars", document_name, len(text))
        return PageContent(
            document_name=document_name,
            document_path=str(document_path),
            page_number=1,
            total_pages=1,
            text=text,
            page_type=PageType.IMAGE,
        )

    @classmethod
    def _ocr_pdf(cls, pdf_path: Path, job: OcrJob) -> list[PageContent]:
        with pymupdf.open(pdf_path) as doc:
            pages = ((num, cls._render_pdf_page(doc, num)) for num in job.page_numbers)
            return cls._ocr_pages(pages, job)

    @classmethod
    def _ocr_tiff(cls, tiff_path: Path, job: OcrJob) -> list[PageContent]:
        def frames() -> Iterator[tuple[int, Image.Image]]:
            with Image.open(tiff_path) as im:
                for page_num in job.page_numbers:
                    im.seek(page_num - 1)
                    yield page_num, im.copy().convert("RGB")

        return cls._ocr_pages(frames(), job)

    @classmethod
    def _ocr_pages(
        cls, pages: Iterator[tuple[int, Image.Image]], job: OcrJob
    ) -> list[PageContent]:
        """OCR a sequence of (page_number, image) pairs."""
        engine = OcrEngine.get()
        results: list[PageContent] = []
        for page_num, img in pages:
            text = cls._extract_text(engine(img))
            logger.info(
                "OCR page %d/%d of %s: %d chars",
                page_num,
                job.total_pages,
                job.document_name,
                len(text),
            )
            results.append(
                PageContent(
                    document_name=job.document_name,
                    document_path=job.document_path,
                    page_number=page_num,
                    total_pages=job.total_pages,
                    text=text,
                    page_type=PageType.IMAGE,
                )
            )
        return results

    @staticmethod
    def _render_pdf_page(doc: pymupdf.Document, page_number: int) -> Image.Image:
        """Render a 1-indexed PDF page to a PIL Image at 200 DPI."""
        page = doc[page_number - 1]
        pix = page.get_pixmap(dpi=200)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    @classmethod
    def _warn_unavailable(cls, exc: Exception) -> None:
        """Log one warning per process the first time OCR is found unavailable."""
        if cls._warned_unavailable:
            return
        cls._warned_unavailable = True
        logger.warning("OCR unavailable, indexing without OCR: %s", exc)

    @staticmethod
    def _extract_text(result: OcrResult) -> str:
        """Extract text lines from a RapidOCR output."""
        if result.txts is None:
            return ""
        return "\n".join(str(t) for t in result.txts)
