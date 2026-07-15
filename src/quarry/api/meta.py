"""Server-meta contracts: liveness and aggregate status."""

from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """The ``GET /health`` snapshot (unversioned; auth-exempt).

    Mirrors the daemon's live ``/health`` body: liveness (``status``), process
    uptime, warm/ready ``state``, the wire ``api_version`` a client negotiates
    against, and the running ``quarry_version``.
    """

    status: str
    uptime_seconds: float
    state: str
    api_version: str
    quarry_version: str


class StatusResponse(BaseModel):
    """The ``GET /status`` aggregate over the daemon's single database."""

    document_count: int
    collection_count: int
    chunk_count: int
    registered_directories: int
    database_path: str
    database_size_bytes: int
    embedding_model: str
    provider: str
    embedding_dimension: int
