"""Async task envelopes: the 202 acceptance and the pollable task status."""

from __future__ import annotations

from pydantic import BaseModel


class TaskAccepted(BaseModel):
    """The 202 body every long-running operation returns on acceptance."""

    task_id: str
    status: str = "accepted"


class TaskStatus(BaseModel):
    """A task's pollable state from ``GET /tasks/{task_id}``.

    ``results`` is populated only on ``completed`` and ``error`` only on
    ``failed``; both are omitted otherwise (the route serializes with
    ``exclude_none`` to match the daemon's conditional shape).
    """

    task_id: str
    status: str
    # wire boundary — the completed result is the operation's own response dict.
    results: dict[str, object] | None = None  # present only when completed
    error: str | None = None  # present only when failed
