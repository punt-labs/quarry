"""The ``/collections`` contract: listing and deletion."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DeleteCollectionRequest(BaseModel):
    """Query parameters for ``DELETE /collections``."""

    name: str


class CollectionInfo(BaseModel):
    """One collection's document and chunk counts.

    ``extra="allow"`` keeps the model a superset of the engine catalog row.
    """

    model_config = ConfigDict(extra="allow")

    collection: str
    document_count: int
    chunk_count: int


class CollectionList(BaseModel):
    """The ``GET /collections`` response envelope."""

    total_collections: int
    collections: list[CollectionInfo]
