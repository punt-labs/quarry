from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from ocr.chunker import chunk_pages
from ocr.config import Settings
from ocr.database import delete_document, insert_chunks
from ocr.embeddings import embed_texts
from ocr.models import PageContent, PageType
from ocr.ocr_client import ocr_pdf_pages
from ocr.pdf_analyzer import analyze_pdf
from ocr.text_extractor import extract_text_pages
from ocr.types import LanceDB

logger = logging.getLogger(__name__)


def ingest_document(
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
    if not file_path.exists():
        msg = f"File not found: {file_path}"
        raise FileNotFoundError(msg)

    def _progress(message: str) -> None:
        logger.info(message)
        if progress_callback is not None:
            progress_callback(message)

    _progress(f"Analyzing: {file_path.name}")

    if overwrite:
        delete_document(db, file_path.name)

    # Step 1: Analyze pages
    analyses = analyze_pdf(file_path)
    total_pages = len(analyses)

    text_pages = [a.page_number for a in analyses if a.page_type == PageType.TEXT]
    image_pages = [a.page_number for a in analyses if a.page_type == PageType.IMAGE]

    _progress(
        f"Pages: {total_pages} total, {len(text_pages)} text, {len(image_pages)} image"
    )

    # Step 2: Extract text and OCR
    all_pages: list[PageContent] = []

    if text_pages:
        _progress(f"Extracting text from {len(text_pages)} pages")
        extracted = extract_text_pages(file_path, text_pages, total_pages)
        all_pages.extend(extracted)

    if image_pages:
        _progress(f"Running OCR on {len(image_pages)} pages via Textract")
        ocr_results = ocr_pdf_pages(file_path, image_pages, total_pages, settings)
        all_pages.extend(ocr_results)

    all_pages.sort(key=lambda p: p.page_number)

    # Step 3: Chunk
    _progress("Chunking pages")
    chunks = chunk_pages(
        all_pages,
        max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars,
    )
    _progress(f"Created {len(chunks)} chunks from {len(all_pages)} pages")

    if not chunks:
        _progress("No text extracted — nothing to index")
        return {
            "document_name": file_path.name,
            "total_pages": total_pages,
            "text_pages": len(text_pages),
            "image_pages": len(image_pages),
            "chunks": 0,
        }

    # Step 4: Embed
    _progress(f"Generating embeddings ({settings.embedding_model})")
    texts = [c.text for c in chunks]
    vectors = embed_texts(texts, model_name=settings.embedding_model)

    # Step 5: Store
    _progress("Storing in LanceDB")
    inserted = insert_chunks(db, chunks, vectors)

    _progress(f"Done: {inserted} chunks indexed from {file_path.name}")

    return {
        "document_name": file_path.name,
        "total_pages": total_pages,
        "text_pages": len(text_pages),
        "image_pages": len(image_pages),
        "chunks": inserted,
    }
