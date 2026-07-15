"""The ``/documents`` contract: listing and deletion."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DeleteDocumentRequest(BaseModel):
    """Query parameters for ``DELETE /documents``."""

    name: str
    collection: str = ""


class DocumentInfo(BaseModel):
    """One indexed document's catalog summary.

    ``extra="allow"`` mirrors the engine catalog row so a new summary field
    passes through untouched (bug-class-3 field parity).
    """

    model_config = ConfigDict(extra="allow")

    document_name: str
    document_path: str
    collection: str
    total_pages: int
    chunk_count: int
    indexed_pages: int
    ingestion_timestamp: str


class DocumentList(BaseModel):
    """The ``GET /documents`` response envelope."""

    total_documents: int
    documents: list[DocumentInfo]
