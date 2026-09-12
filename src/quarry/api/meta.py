"""Server-meta contracts: liveness and aggregate status."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FdHealth(BaseModel):
    """The daemon's open file descriptors against its soft ``RLIMIT_NOFILE``.

    Sampled inside the daemon process, so it is the resident daemon's own fd
    state — the number a health check must report, never the short-lived CLI's.
    """

    open_fds: int = Field(ge=0)
    soft_limit: int = Field(gt=0)


class HealthResponse(BaseModel):
    """The daemon's liveness and readiness snapshot (unversioned; auth-exempt).

    Carries liveness (``status``), process uptime, warm/ready ``state``, the wire
    ``api_version`` a client negotiates against, the running ``quarry_version``,
    and the daemon's own file-descriptor headroom.
    """

    status: str
    uptime_seconds: float
    state: str
    api_version: str
    quarry_version: str
    # ``None`` in two benign cases. The daemon's own fd scan raised — EMFILE, or
    # a platform with no fd directory — so it emits ``fd: null`` rather than
    # 500-ing the endpoint on the exhaustion this field exists to surface. Or a
    # client parses an older daemon whose response predates this field, and the
    # default maps that absent key to ``None`` (the upgrade-before-reinstall window).
    fd: FdHealth | None = None


class StatusResponse(BaseModel):
    """The aggregate status over the daemon's single database."""

    document_count: int
    collection_count: int
    chunk_count: int
    registered_directories: int
    database_path: str
    embedding_model: str
    provider: str
    embedding_dimension: int


class CoverageResponse(BaseModel):
    """Per-repo coverage of a collection and its ``<collection>-captures`` sibling.

    Three distinct-name counts scoped to one repo's slice of the shared catalog:
    non-memory documents in ``collection``, ``session-<id>`` compaction archives
    in ``captures_collection``, and memory rows (``agent_handle``/``memory_type``
    set) in ``collection``. A URL capture in the captures collection is neither
    — it's a background artifact and not surfaced here.
    """

    documents_indexed: int
    transcripts_captured: int
    memories_saved: int
