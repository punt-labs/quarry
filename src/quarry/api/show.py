"""The show contract: fetch document metadata or a single page's text."""

from __future__ import annotations

from pydantic import BaseModel


class ShowRequest(BaseModel):
    """Query parameters for a document show request.

    Omit ``page`` to get the document's catalog metadata (a ``DocumentInfo``);
    send ``page >= 1`` for that page's text (a ``ShowPageResponse``). A present
    ``page < 1`` is rejected with 400.
    """

    document: str
    collection: str = ""
    page: int | None = None  # omit -> metadata; >=1 -> page text; <1 -> 400


class ShowPageResponse(BaseModel):
    """A single page's text, returned when ``page >= 1``."""

    document_name: str
    page_number: int
    text: str
