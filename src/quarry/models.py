"""Immutable data models: PageContent, Chunk, PageAnalysis, etc."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime


class PageType(enum.Enum):
    TEXT = "text"
    IMAGE = "image"
    SECTION = "section"
    CODE = "code"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"

    @property
    def stored(self) -> str:
        """Return the string stored in LanceDB for this page type."""
        mapping: dict[PageType, str] = {
            PageType.CODE: "code",
            PageType.SPREADSHEET: "spreadsheet",
            PageType.PRESENTATION: "presentation",
        }
        return mapping.get(self, "text")


@dataclass(frozen=True, slots=True)
class PageAnalysis:
    page_number: int
    page_type: PageType
    text_length: int


@dataclass(frozen=True, slots=True)
class PageContent:
    """Content from one page or logical section of a document.

    For PDFs, page_number and total_pages are physical page indices.
    For text/code files, they represent logical section indices (e.g.
    markdown heading, code definition) — "page" means "section" there.
    For presentations, each slide is one page.
    """

    document_name: str
    document_path: str
    page_number: int  # Physical page (PDF) or logical section index (text/code)
    total_pages: int
    text: str
    page_type: PageType


@dataclass(frozen=True, slots=True)
class Chunk:
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
    ingestion_timestamp: datetime
    agent_handle: str = ""
    memory_type: str = ""
    summary: str = ""


@dataclass(frozen=True, slots=True)
class SitemapOptions:
    """Options for sitemap-based ingestion."""

    include: str | None = None
    exclude: str | None = None
    limit: int | None = None
    workers: int = 4
    delay: float = 0.0
    timeout: float = 30.0


@dataclass(frozen=True, slots=True)
class ChunkConfig:
    """Configuration for chunk creation during ingestion."""

    agent_handle: str | None = None
    memory_type: str | None = None
    summary: str | None = None
    max_chars: int = 2000
    overlap_chars: int = 200
    collection: str = "default"
    source_format: str = ""
