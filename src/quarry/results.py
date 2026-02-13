"""Typed result structures for database and pipeline operations."""

from __future__ import annotations

from typing import TypedDict


class SearchResult(TypedDict):
    """A single search hit from vector similarity search.

    All keys except _distance come from stored chunk metadata.
    _distance is added by LanceDB (lower = more similar).
    """

    document_name: str
    document_path: str
    collection: str
    page_number: int
    total_pages: int
    chunk_index: int
    text: str
    page_raw_text: str
    ingestion_timestamp: str
    _distance: float


class DocumentSummary(TypedDict):
    """Summary of an indexed document from list_documents."""

    document_name: str
    document_path: str
    collection: str
    total_pages: int
    chunk_count: int
    indexed_pages: int
    ingestion_timestamp: str


class CollectionSummary(TypedDict):
    """Summary of a collection from list_collections."""

    collection: str
    document_count: int
    chunk_count: int
