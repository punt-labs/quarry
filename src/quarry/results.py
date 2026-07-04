"""Typed result structures for database and pipeline operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NotRequired, TypedDict

from quarry._sql import escape_sql


class IngestResult(TypedDict):
    """Result of document ingestion.

    Common fields: document_name, collection, chunks. Format-specific fields
    (total_pages, sections, format, etc.) are populated per handler.
    """

    document_name: str
    collection: str
    chunks: int
    total_pages: NotRequired[int]
    text_pages: NotRequired[int]
    image_pages: NotRequired[int]
    sections: NotRequired[int]
    definitions: NotRequired[int]
    sheets: NotRequired[int]
    slides: NotRequired[int]
    format: NotRequired[str]


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
    page_type: str
    source_format: str
    ingestion_timestamp: str
    agent_handle: str
    memory_type: str
    summary: str
    _distance: float


def result_similarity(row: SearchResult) -> float:
    """Return a row's cosine similarity from its ``_distance``.

    Under the cosine metric ``_distance = 1 - cos(θ)``, so similarity is
    ``1 - _distance`` in ``[-1, 1]``. A row missing ``_distance`` defaults to
    the worst-case distance ``2.0`` (similarity ``-1``), so it sinks to the
    bottom rather than surfacing as a fake perfect ``1.0`` (quarry-gcnf).
    """
    return round(1.0 - float(str(row.get("_distance", 2.0))), 4)


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


class SitemapResult(TypedDict):
    """Result of sitemap crawl and bulk URL ingestion."""

    sitemap_url: str
    collection: str
    total_discovered: int
    after_filter: int
    ingested: int
    skipped: int
    failed: int
    errors: list[str]


class DatabaseSummary(TypedDict):
    """Summary of a named database from the databases command."""

    name: str
    document_count: int
    size_bytes: int
    size_description: str


# ---------------------------------------------------------------------------
# Filter bundling for hybrid search predicates
# ---------------------------------------------------------------------------

_FILTER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("collection", "collection"),
    ("document", "document_name"),
    ("page_type", "page_type"),
    ("source_format", "source_format"),
    ("agent_handle", "agent_handle"),
    ("memory_type", "memory_type"),
)


@dataclass(frozen=True, slots=True)
class SearchFilter:
    """Bundle of optional filters for hybrid search predicates."""

    collection: str | None = None
    document: str | None = None
    page_type: str | None = None
    source_format: str | None = None
    agent_handle: str | None = None
    memory_type: str | None = None

    def to_predicate(self) -> str | None:
        """Return a SQL WHERE clause string, or None if no filters are set."""
        parts: list[str] = []
        for field_name, column_name in _FILTER_COLUMNS:
            value = getattr(self, field_name)
            if value:
                parts.append(f"{column_name} = '{escape_sql(value)}'")
        return " AND ".join(parts) if parts else None
