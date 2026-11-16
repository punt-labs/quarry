"""Wire shapes the daemon reports on ``/health``: fd headroom and finalize."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FdHealth(BaseModel):
    """The daemon's open file descriptors against its soft ``RLIMIT_NOFILE``.

    Sampled inside the daemon process, so it is the resident daemon's own fd
    state — the number a health check must report, never the short-lived CLI's.
    """

    open_fds: int = Field(ge=0)
    soft_limit: int = Field(gt=0)


class FinalizeDatabaseHealth(BaseModel):
    """One database's finalize-optimize failure ledger.

    ``consecutive_failures`` is 0 when the last optimize succeeded (or none has
    failed yet); otherwise ``last_error`` is its reason and ``retry_in_seconds``
    is how long the backoff window still has to run.
    """

    database: str
    consecutive_failures: int = Field(ge=0)
    last_error: str
    retry_in_seconds: float = Field(ge=0)


class FinalizeHealth(BaseModel):
    """The finalize-optimize ledger of every database the daemon has finalized."""

    databases: list[FinalizeDatabaseHealth]
