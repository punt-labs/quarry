"""Server-meta contracts: liveness and aggregate status."""

from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """The ``GET /health`` snapshot (unversioned; auth-exempt).

    ``state`` is ``ready`` once the engine is warm; ``api_version`` lets a
    client detect a major wire-protocol mismatch before issuing requests.
    """

    status: str
    uptime_seconds: float
    state: str
    api_version: str
    quarry_version: str


class StatusResponse(BaseModel):
    """The ``GET /v1/status`` aggregate over the daemon's single database."""

    document_count: int
    collection_count: int
    chunk_count: int
    registered_directories: int
    database_path: str
    database_size_bytes: int
    embedding_model: str
    provider: str
    embedding_dimension: int
