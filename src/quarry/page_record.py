"""The stored text of one page."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PageRecord:
    """The full text of one page, stored once however many chunks it yields."""

    collection: str
    document_name: str
    page_number: int
    text: str
