from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from quarry.chunker import chunk_pages
from quarry.config import Settings
from quarry.database import delete_document, insert_chunks
from quarry.embeddings import embed_texts
from quarry.image_analyzer import (
    SUPPORTED_IMAGE_EXTENSIONS,
    analyze_image,
)
from quarry.models import PageContent, PageType
from quarry.ocr_client import ocr_document_via_s3, ocr_image_bytes
from quarry.pdf_analyzer import analyze_pdf
from quarry.text_extractor import extract_text_pages
from quarry.text_processor import (
    SUPPORTED_TEXT_EXTENSIONS,
    process_raw_text,
    process_text_file,
)
from quarry.types import LanceDB

logger = logging.getLogger(__name__)


def ingest_document(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Ingest a document: dispatch to format-specific handler.

    Supported formats: PDF, TXT, MD, TEX, DOCX, PNG, JPEG, TIFF, BMP, WebP.

    Args:
        file_path: Path to the document.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.

    Raises:
        FileNotFoundError: If file does not exist.
        ValueError: If file format is not supported.
    """
    if not file_path.exists():
        msg = f"File not found: {file_path}"
        raise FileNotFoundError(msg)

    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return ingest_pdf(
            file_path,
            db,
            settings,
            overwrite=overwrite,
            progress_callback=progress_callback,
        )

    if suffix in SUPPORTED_TEXT_EXTENSIONS:
        return ingest_text_file(
            file_path,
            db,
            settings,
            overwrite=overwrite,
            progress_callback=progress_callback,
        )

    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        return ingest_image(
            file_path,
            db,
            settings,
            overwrite=overwrite,
            progress_callback=progress_callback,
        )

    msg = f"Unsupported file format: {suffix}"
    raise ValueError(msg)


def ingest_pdf(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Ingest a PDF document: analyze, extract/OCR, chunk, embed, store.

    Args:
        file_path: Path to the PDF file.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results (pages, chunks, etc).
    """
    progress = _make_progress(progress_callback)

    progress("Analyzing: %s", file_path.name)

    if overwrite:
        delete_document(db, file_path.name)

    analyses = analyze_pdf(file_path)
    total_pages = len(analyses)

    text_pages = [a.page_number for a in analyses if a.page_type == PageType.TEXT]
    image_pages = [a.page_number for a in analyses if a.page_type == PageType.IMAGE]

    progress(
        "Pages: %d total, %d text, %d image",
        total_pages,
        len(text_pages),
        len(image_pages),
    )

    all_pages: list[PageContent] = []

    if text_pages:
        progress("Extracting text from %d pages", len(text_pages))
        extracted = extract_text_pages(file_path, text_pages, total_pages)
        all_pages.extend(extracted)

    if image_pages:
        progress("Running OCR on %d pages via Textract", len(image_pages))
        ocr_results = ocr_document_via_s3(file_path, image_pages, total_pages, settings)
        all_pages.extend(ocr_results)

    all_pages.sort(key=lambda p: p.page_number)

    return _chunk_embed_store(
        all_pages,
        file_path.name,
        db,
        settings,
        progress,
        extra={
            "total_pages": total_pages,
            "text_pages": len(text_pages),
            "image_pages": len(image_pages),
        },
    )


def ingest_text_file(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Ingest a text document: read, split into sections, chunk, embed, store.

    Supported: .txt, .md, .tex, .docx.

    Args:
        file_path: Path to the text file.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)

    progress("Reading: %s", file_path.name)

    if overwrite:
        delete_document(db, file_path.name)

    pages = process_text_file(file_path)
    progress("Sections: %d", len(pages))

    return _chunk_embed_store(
        pages,
        file_path.name,
        db,
        settings,
        progress,
        extra={"sections": len(pages)},
    )


def ingest_image(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Ingest a standalone image: OCR, chunk, embed, store.

    Supported: PNG, JPEG, TIFF (multi-page), BMP, WebP.
    BMP and WebP are converted to PNG before OCR.

    Single-page images use Textract sync API (DetectDocumentText).
    Multi-page TIFFs use Textract async API via S3.

    Args:
        file_path: Path to image file.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)

    progress("Analyzing image: %s", file_path.name)

    if overwrite:
        delete_document(db, file_path.name)

    analysis = analyze_image(file_path)
    progress(
        "Image: %s, %d pages, conversion=%s",
        analysis.format,
        analysis.page_count,
        analysis.needs_conversion,
    )

    if analysis.page_count > 1:
        return _ingest_multipage_image(
            file_path, analysis.page_count, db, settings, progress
        )

    image_bytes = _prepare_image_bytes(
        file_path, needs_conversion=analysis.needs_conversion
    )
    page = ocr_image_bytes(
        image_bytes,
        document_name=file_path.name,
        document_path=str(file_path.resolve()),
    )

    return _chunk_embed_store(
        [page],
        file_path.name,
        db,
        settings,
        progress,
        extra={"format": analysis.format, "image_pages": 1},
    )


def _prepare_image_bytes(image_path: Path, *, needs_conversion: bool) -> bytes:
    """Read image bytes, converting BMP/WebP to PNG if needed."""
    if not needs_conversion:
        return image_path.read_bytes()

    import io  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    with Image.open(image_path) as im:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()


def _ingest_multipage_image(
    file_path: Path,
    page_count: int,
    db: LanceDB,
    settings: Settings,
    progress: Callable[..., None],
) -> dict[str, object]:
    """Ingest a multi-page image (TIFF) via async Textract API."""
    progress("Running OCR on %d pages via Textract (async)", page_count)
    all_page_numbers = list(range(1, page_count + 1))
    pages = ocr_document_via_s3(file_path, all_page_numbers, page_count, settings)

    return _chunk_embed_store(
        pages,
        file_path.name,
        db,
        settings,
        progress,
        extra={"format": "TIFF", "image_pages": page_count},
    )


def ingest_text(
    text: str,
    document_name: str,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    format_hint: str = "auto",
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Ingest raw text: split into sections, chunk, embed, store.

    Args:
        text: Raw text content.
        document_name: Name for the document.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        format_hint: One of 'auto', 'plain', 'markdown', 'latex'.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)

    progress("Processing: %s", document_name)

    if overwrite:
        delete_document(db, document_name)

    pages = process_raw_text(text, document_name, format_hint=format_hint)
    progress("Sections: %d", len(pages))

    return _chunk_embed_store(
        pages,
        document_name,
        db,
        settings,
        progress,
        extra={"sections": len(pages)},
    )


def _make_progress(
    callback: Callable[[str], None] | None,
) -> Callable[..., None]:
    """Create a progress reporter that logs and optionally calls a callback."""

    def _progress(fmt: str, *args: object) -> None:
        logger.info(fmt, *args)
        if callback is not None:
            callback(fmt % args if args else fmt)

    return _progress


def _chunk_embed_store(
    pages: list[PageContent],
    document_name: str,
    db: LanceDB,
    settings: Settings,
    progress: Callable[..., None],
    *,
    extra: dict[str, object],
) -> dict[str, object]:
    """Shared pipeline: chunk pages, embed, store in LanceDB.

    Args:
        pages: Page contents to process.
        document_name: Document identifier.
        db: LanceDB connection.
        settings: Application settings.
        progress: Progress reporter.
        extra: Additional fields for the result dict.

    Returns:
        Dict with document_name, chunks count, and extra fields.
    """
    progress("Chunking")
    chunks = chunk_pages(
        pages,
        max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars,
    )
    progress("Created %d chunks", len(chunks))

    if not chunks:
        progress("No text found — nothing to index")
        return {"document_name": document_name, "chunks": 0, **extra}

    progress("Generating embeddings (%s)", settings.embedding_model)
    texts = [c.text for c in chunks]
    vectors = embed_texts(texts, model_name=settings.embedding_model)

    progress("Storing in LanceDB")
    inserted = insert_chunks(db, chunks, vectors)

    progress("Done: %d chunks indexed from %s", inserted, document_name)

    return {"document_name": document_name, "chunks": inserted, **extra}
