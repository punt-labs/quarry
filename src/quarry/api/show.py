"""The ``/v1/show`` contract: fetch document metadata or a single page's text."""

from __future__ import annotations

from pydantic import BaseModel


class ShowRequest(BaseModel):
    """Query parameters for ``GET /v1/show``.

    With ``page`` unset (or 0) the endpoint returns the document's catalog
    metadata (a ``DocumentInfo``); with ``page >= 1`` it returns that page's
    text (a ``ShowPageResponse``).
    """

    document: str
    collection: str = ""
    page: int | None = None  # None/0 -> metadata; >=1 -> page text


class ShowPageResponse(BaseModel):
    """A single page's text, returned when ``page >= 1``."""

    document_name: str
    page_number: int
    text: str
