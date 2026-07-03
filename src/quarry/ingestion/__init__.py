"""Ingestion subsystem: document loading, chunking, embedding, and storage."""

from __future__ import annotations

from quarry.ingestion.backends import (
    clear_caches,
    get_embedding_backend,
    get_ocr_backend,
)
from quarry.ingestion.chunker import chunk_pages
from quarry.ingestion.image_preparer import ImagePreparer
from quarry.ingestion.ocr_local import LocalOcrBackend, get_engine
from quarry.ingestion.pdf_text_extractor import extract_text_pages
from quarry.ingestion.pipeline import (
    SUPPORTED_EXTENSIONS,
    ingest_auto,
    ingest_content,
    ingest_document,
    ingest_sitemap,
    ingest_url,
)
from quarry.ingestion.provider import PROVIDER_MODEL_MAP, ProviderSelection
from quarry.ingestion.text_splitter import (
    read_text_with_fallback,
    sections_to_pages,
    split_latex,
    split_markdown,
    split_plain,
)
from quarry.ingestion.url_fetcher import UrlFetcher
from quarry.ingestion.url_ingester import UrlIngester

__all__ = [
    "PROVIDER_MODEL_MAP",
    "SUPPORTED_EXTENSIONS",
    "ImagePreparer",
    "LocalOcrBackend",
    "ProviderSelection",
    "UrlFetcher",
    "UrlIngester",
    "chunk_pages",
    "clear_caches",
    "extract_text_pages",
    "get_embedding_backend",
    "get_engine",
    "get_ocr_backend",
    "ingest_auto",
    "ingest_content",
    "ingest_document",
    "ingest_sitemap",
    "ingest_url",
    "read_text_with_fallback",
    "sections_to_pages",
    "split_latex",
    "split_markdown",
    "split_plain",
]
