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


def stored_page_type(pt: PageType) -> str:
    """Map a PageType enum to the string stored in LanceDB.

    TEXT, IMAGE, and SECTION all represent prose content and map to
    ``"text"``.  CODE maps to ``"code"``.  SPREADSHEET maps to
    ``"spreadsheet"``.  PRESENTATION maps to ``"presentation"``.
    """
    mapping: dict[PageType, str] = {
        PageType.CODE: "code",
        PageType.SPREADSHEET: "spreadsheet",
        PageType.PRESENTATION: "presentation",
    }
    return mapping.get(pt, "text")


@dataclass(frozen=True)
class PageAnalysis:
    page_number: int
    page_type: PageType
    text_length: int


@dataclass(frozen=True)
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


@dataclass(frozen=True)
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
