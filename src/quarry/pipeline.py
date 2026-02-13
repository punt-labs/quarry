from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

from quarry.backends import get_embedding_backend, get_ocr_backend
from quarry.chunker import chunk_pages
from quarry.code_processor import SUPPORTED_CODE_EXTENSIONS, process_code_file
from quarry.config import Settings
from quarry.database import delete_document, insert_chunks
from quarry.image_analyzer import (
    SUPPORTED_IMAGE_EXTENSIONS,
    analyze_image,
)
from quarry.models import PageContent, PageType
from quarry.pdf_analyzer import analyze_pdf
from quarry.text_extractor import extract_text_pages
from quarry.text_processor import (
    SUPPORTED_TEXT_EXTENSIONS,
    process_raw_text,
    process_text_file,
)
from quarry.types import LanceDB

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = (
    frozenset({".pdf"})
    | SUPPORTED_TEXT_EXTENSIONS
    | SUPPORTED_IMAGE_EXTENSIONS
    | SUPPORTED_CODE_EXTENSIONS
)


def ingest_document(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Ingest a document: dispatch to format-specific handler.

    Supported formats: PDF, TXT, MD, TEX, DOCX, PNG, JPEG, TIFF, BMP, WebP.

    Args:
        file_path: Path to the document.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name. Defaults to
            ``file_path.name``.  Pass a relative path (e.g. from
            ``file_path.relative_to(root)``) to avoid collisions when
            syncing directories with identically-named files in
            subdirectories.
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
            collection=collection,
            document_name=document_name,
            progress_callback=progress_callback,
        )

    if suffix in SUPPORTED_CODE_EXTENSIONS:
        return ingest_code_file(
            file_path,
            db,
            settings,
            overwrite=overwrite,
            collection=collection,
            document_name=document_name,
            progress_callback=progress_callback,
        )

    if suffix in SUPPORTED_TEXT_EXTENSIONS:
        return ingest_text_file(
            file_path,
            db,
            settings,
            overwrite=overwrite,
            collection=collection,
            document_name=document_name,
            progress_callback=progress_callback,
        )

    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        return ingest_image(
            file_path,
            db,
            settings,
            overwrite=overwrite,
            collection=collection,
            document_name=document_name,
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
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Ingest a PDF document: analyze, extract/OCR, chunk, embed, store.

    Args:
        file_path: Path to the PDF file.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results (pages, chunks, etc).
    """
    progress = _make_progress(progress_callback)
    doc_name = document_name or file_path.name

    progress("Analyzing: %s", doc_name)

    if overwrite:
        delete_document(db, doc_name, collection=collection)

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
        extracted = extract_text_pages(
            file_path, text_pages, total_pages, document_name=doc_name
        )
        all_pages.extend(extracted)

    if image_pages:
        progress("Running OCR on %d pages", len(image_pages))
        ocr = get_ocr_backend(settings)
        ocr_results = ocr.ocr_document(
            file_path, image_pages, total_pages, document_name=doc_name
        )
        all_pages.extend(ocr_results)

    all_pages.sort(key=lambda p: p.page_number)

    return _chunk_embed_store(
        all_pages,
        doc_name,
        db,
        settings,
        progress,
        collection=collection,
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
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Ingest a text document: read, split into sections, chunk, embed, store.

    Supported: .txt, .md, .tex, .docx.

    Args:
        file_path: Path to the text file.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)
    doc_name = document_name or file_path.name

    progress("Reading: %s", doc_name)

    if overwrite:
        delete_document(db, doc_name, collection=collection)

    pages = process_text_file(file_path)
    progress("Sections: %d", len(pages))

    return _chunk_embed_store(
        pages,
        doc_name,
        db,
        settings,
        progress,
        collection=collection,
        extra={"sections": len(pages)},
    )


def ingest_code_file(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Ingest source code: parse into definitions, chunk, embed, store.

    Uses tree-sitter for language-aware splitting when available.

    Args:
        file_path: Path to the source code file.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)
    doc_name = document_name or file_path.name

    progress("Parsing: %s", doc_name)

    if overwrite:
        delete_document(db, doc_name, collection=collection)

    pages = process_code_file(file_path)
    progress("Definitions: %d", len(pages))

    return _chunk_embed_store(
        pages,
        doc_name,
        db,
        settings,
        progress,
        collection=collection,
        extra={"definitions": len(pages)},
    )


def ingest_image(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
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
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)
    doc_name = document_name or file_path.name

    progress("Analyzing image: %s", doc_name)

    if overwrite:
        delete_document(db, doc_name, collection=collection)

    analysis = analyze_image(file_path)
    progress(
        "Image: %s, %d pages, conversion=%s",
        analysis.format,
        analysis.page_count,
        analysis.needs_conversion,
    )

    if analysis.page_count > 1:
        return _ingest_multipage_image(
            file_path,
            analysis.page_count,
            db,
            settings,
            progress,
            document_name=doc_name,
            collection=collection,
        )

    image_bytes = _prepare_image_bytes(
        file_path,
        needs_conversion=analysis.needs_conversion,
        max_bytes=settings.textract_max_image_bytes,
    )
    ocr = get_ocr_backend(settings)
    page = ocr.ocr_image_bytes(
        image_bytes,
        document_name=doc_name,
        document_path=file_path.resolve(),
    )

    return _chunk_embed_store(
        [page],
        doc_name,
        db,
        settings,
        progress,
        collection=collection,
        extra={"format": analysis.format, "image_pages": 1},
    )


def _prepare_image_bytes(
    image_path: Path,
    *,
    needs_conversion: bool,
    max_bytes: int = 0,
) -> bytes:
    """Read image bytes, converting format and/or downscaling as needed.

    Format conversion: MPO → JPEG, BMP/WebP → PNG, with EXIF transpose.
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

        return _encode_image_to_fit(img, out_fmt, save_kw, max_bytes, image_path.name)


def _encode_image_to_fit(
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

    if max_bytes > 0 and len(data) > max_bytes:
        logger.warning(
            "%s still %d bytes after downscaling (limit %d)",
            name,
            len(data),
            max_bytes,
        )

    return data


def _ingest_multipage_image(
    file_path: Path,
    page_count: int,
    db: LanceDB,
    settings: Settings,
    progress: Callable[..., None],
    *,
    document_name: str | None = None,
    collection: str = "default",
) -> dict[str, object]:
    """Ingest a multi-page image (TIFF) via async Textract API."""
    doc_name = document_name or file_path.name
    progress("Running OCR on %d pages (async)", page_count)
    all_page_numbers = list(range(1, page_count + 1))
    ocr = get_ocr_backend(settings)
    pages = ocr.ocr_document(
        file_path, all_page_numbers, page_count, document_name=doc_name
    )

    return _chunk_embed_store(
        pages,
        doc_name,
        db,
        settings,
        progress,
        collection=collection,
        extra={"format": "TIFF", "image_pages": page_count},
    )


def ingest_text_content(
    text: str,
    document_name: str,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
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
        collection: Collection name for organizing documents.
        format_hint: One of 'auto', 'plain', 'markdown', 'latex'.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)

    progress("Processing: %s", document_name)

    if overwrite:
        delete_document(db, document_name, collection=collection)

    pages = process_raw_text(text, document_name, format_hint=format_hint)
    progress("Sections: %d", len(pages))

    return _chunk_embed_store(
        pages,
        document_name,
        db,
        settings,
        progress,
        collection=collection,
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
    collection: str = "default",
    extra: dict[str, object],
) -> dict[str, object]:
    """Shared pipeline: chunk pages, embed, store in LanceDB.

    Args:
        pages: Page contents to process.
        document_name: Document identifier.
        db: LanceDB connection.
        settings: Application settings.
        progress: Progress reporter.
        collection: Collection name for the chunks.
        extra: Additional fields for the result dict.

    Returns:
        Dict with document_name, collection, chunks count, and extra fields.
    """
    progress("Chunking")
    chunks = chunk_pages(
        pages,
        max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars,
        collection=collection,
    )
    progress("Created %d chunks", len(chunks))

    if not chunks:
        progress("No text found — nothing to index")
        return {
            "document_name": document_name,
            "collection": collection,
            "chunks": 0,
            **extra,
        }

    embedder = get_embedding_backend(settings)
    progress("Generating embeddings (%s)", embedder.model_name)
    texts = [c.text for c in chunks]
    vectors = embedder.embed_texts(texts)

    progress("Storing in LanceDB")
    inserted = insert_chunks(db, chunks, vectors)

    progress("Done: %d chunks indexed from %s", inserted, document_name)

    return {
        "document_name": document_name,
        "collection": collection,
        "chunks": inserted,
        **extra,
    }
