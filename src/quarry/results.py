"""Typed result structures for database and pipeline operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import NotRequired, Self, TypedDict

from quarry._sql import escape_sql

# Cosine distance for a missing/uncomputable vector -> similarity -1, sinks to
# the bottom. Single source of truth for the worst-case sentinel (quarry-gcnf).
WORST_CASE_DISTANCE: float = 2.0


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


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A single search hit with the metadata needed to display and rank it.

    ``distance`` is the cosine distance LanceDB reports for the vector channel
    (``_distance``); FTS-only rows receive it from ``_annotate_fts_distances``.
    A missing distance defaults to :data:`WORST_CASE_DISTANCE`. Build instances
    with :meth:`from_row` from a LanceDB or FTS row mapping.
    """

    document_name: str
    collection: str
    page_number: int
    chunk_index: int
    text: str
    page_type: str
    source_format: str
    agent_handle: str
    memory_type: str
    summary: str
    distance: float

    @property
    def similarity(self) -> float:
        """Return cosine similarity in ``[-1, 1]``: ``1 - distance``, rounded.

        A row with the worst-case distance sinks to ``-1`` rather than
        surfacing as a fake perfect ``1.0`` (quarry-gcnf).
        """
        return round(1.0 - self.distance, 4)

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> Self:
        """Build a result from a LanceDB/FTS row mapping.

        Missing string fields default to ``""``, missing counts to ``0``, and a
        missing ``_distance`` to :data:`WORST_CASE_DISTANCE`.
        """
        return cls(
            document_name=cls._as_text(row, "document_name"),
            collection=cls._as_text(row, "collection"),
            page_number=cls._as_count(row, "page_number"),
            chunk_index=cls._as_count(row, "chunk_index"),
            text=cls._as_text(row, "text"),
            page_type=cls._as_text(row, "page_type"),
            source_format=cls._as_text(row, "source_format"),
            agent_handle=cls._as_text(row, "agent_handle"),
            memory_type=cls._as_text(row, "memory_type"),
            summary=cls._as_text(row, "summary"),
            distance=cls._as_distance(row),
        )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready mapping including the derived similarity."""
        return {
            "document_name": self.document_name,
            "collection": self.collection,
            "page_number": self.page_number,
            "chunk_index": self.chunk_index,
            "text": self.text,
            "page_type": self.page_type,
            "source_format": self.source_format,
            "agent_handle": self.agent_handle,
            "memory_type": self.memory_type,
            "summary": self.summary,
            "similarity": self.similarity,
        }

    @staticmethod
    def _as_text(row: Mapping[str, object], key: str) -> str:
        value = row.get(key)
        return "" if value is None else str(value)

    @staticmethod
    def _as_count(row: Mapping[str, object], key: str) -> int:
        value = row.get(key)
        return 0 if value is None else int(str(value))

    @staticmethod
    def _as_distance(row: Mapping[str, object]) -> float:
        value = row.get("_distance")
        return WORST_CASE_DISTANCE if value is None else float(str(value))


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
