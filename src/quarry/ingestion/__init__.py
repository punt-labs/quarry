"""Ingestion subsystem: document loading, chunking, embedding, and storage."""

from __future__ import annotations

from quarry.ingestion.backends import (
    clear_caches,
    get_embedding_backend,
    get_ocr_backend,
)
from quarry.ingestion.chunker import chunk_pages
from quarry.ingestion.ocr_local import LocalOcrBackend
from quarry.ingestion.pdf_text_extractor import extract_text_pages
from quarry.ingestion.pipeline import SUPPORTED_EXTENSIONS, ingest_document
from quarry.ingestion.provider import PROVIDER_MODEL_MAP, ProviderSelection
from quarry.ingestion.section_splitter import SectionSplitter
from quarry.ingestion.sitemap_ingest import ingest_auto, ingest_sitemap
from quarry.ingestion.text_splitter import (
    read_text_with_fallback,
    sections_to_pages,
)
from quarry.ingestion.web_ingest import ingest_content, ingest_url

__all__ = [
    "PROVIDER_MODEL_MAP",
    "SUPPORTED_EXTENSIONS",
    "LocalOcrBackend",
    "ProviderSelection",
    "SectionSplitter",
    "chunk_pages",
    "clear_caches",
    "extract_text_pages",
    "get_embedding_backend",
    "get_ocr_backend",
    "ingest_auto",
    "ingest_content",
    "ingest_document",
    "ingest_sitemap",
    "ingest_url",
    "read_text_with_fallback",
    "sections_to_pages",
]
