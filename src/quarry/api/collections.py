"""The ``/v1/collections`` contract: listing and deletion."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DeleteCollectionRequest(BaseModel):
    """Query parameters for ``DELETE /v1/collections``."""

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
    """The ``GET /v1/collections`` response envelope."""

    total_collections: int
    collections: list[CollectionInfo]
