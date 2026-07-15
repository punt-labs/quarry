"""The ``/search`` contract: query parameters, ranked hits, and response."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SearchRequest(BaseModel):
    """Query parameters for ``GET /search`` (``q`` is the search text)."""

    query: str = Field(alias="q", min_length=1)
    limit: int = 10
    collection: str = ""
    document: str = ""
    page_type: str = ""
    source_format: str = ""
    agent_handle: str = ""
    memory_type: str = ""


class SearchHit(BaseModel):
    """One ranked result row — the ``SearchService`` projection (DES-037).

    ``extra="allow"`` keeps the model a faithful superset of the engine's
    row projection so a new column can never be silently dropped on the wire.
    """

    model_config = ConfigDict(extra="allow")

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
    similarity: float


class SearchResponse(BaseModel):
    """The ``GET /search`` response envelope."""

    query: str
    total_results: int
    results: list[SearchHit]
