from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime


class PageType(enum.Enum):
    TEXT = "text"
    IMAGE = "image"
    SECTION = "section"


@dataclass(frozen=True)
class PageAnalysis:
    page_number: int
    page_type: PageType
    text_length: int


@dataclass(frozen=True)
class PageContent:
    document_name: str
    document_path: str
    page_number: int
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
    ingestion_timestamp: datetime
