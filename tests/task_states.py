"""Task states that reached a terminal status by the state's own moves."""

from __future__ import annotations

from typing import TYPE_CHECKING

from quarry.daemon.task_state import TaskState

if TYPE_CHECKING:
    from collections.abc import Mapping


def completed(
    task_id: str,
    kind: str,
    results: Mapping[str, object] | None = None,  # None: it recorded nothing
    *,
    created_at: float | None = None,  # None: created now
) -> TaskState:
    """Return a task that ended ``completed`` with *results*."""
    state = TaskState(task_id, kind, created_at=created_at)
    state.complete(results or {})
    return state


def failed(
    task_id: str,
    kind: str,
    reason: str = "failed",
    *,
    created_at: float | None = None,  # None: created now
) -> TaskState:
    """Return a task that ended ``failed`` for *reason*."""
    state = TaskState(task_id, kind, created_at=created_at)
    state.fail(reason)
    return state
