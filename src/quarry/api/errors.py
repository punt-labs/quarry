"""The uniform error envelope returned by every daemon endpoint on failure."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ErrorBody(BaseModel):
    """The wire error shape: ``{"error": "..."}`` plus any endpoint extras.

    ``extra="allow"`` preserves the richer error payloads some routes emit —
    e.g. the ``/sync`` 409 conflict adds ``status`` and ``task_id`` — so the
    envelope stays a faithful superset of every current error response.
    """

    model_config = ConfigDict(extra="allow")

    error: str
