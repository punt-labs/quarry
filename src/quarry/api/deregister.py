"""The ``DELETE /registrations`` contract: deregister a collection."""

from __future__ import annotations

from pydantic import BaseModel


class DeregisterRequest(BaseModel):
    """Query parameters for ``DELETE /registrations``.

    ``keep_data`` retains indexed chunks; otherwise the deregistered
    collection's chunks are purged as a background task.
    """

    collection: str
    keep_data: bool = False


class DeregisterAccepted(BaseModel):
    """The 202 body for a deregister — carries the removed-file count.

    Distinct from the plain ``TaskAccepted`` because deregister reports
    ``removed`` synchronously (the registry rows are gone before the chunk
    purge task runs).
    """

    task_id: str
    status: str = "accepted"
    removed: int
