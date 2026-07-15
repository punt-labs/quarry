"""Background-task bookkeeping for the daemon: state, GC, and terminal recording.

Every long-running REST operation (sync, ingest, remember, delete, register,
deregister) runs as an asyncio task and reports progress through a ``TaskState``
that the client polls via ``GET /tasks/{task_id}``.  ``TaskRegistry`` owns
the live and completed states plus their asyncio task handles, evicting
completed states after a TTL so a long-lived daemon does not leak them.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Self, final

logger = logging.getLogger(__name__)

# Task GC: completed/failed tasks are evicted after this many seconds.
TASK_TTL_SECONDS = 3600  # 1 hour


@dataclass(slots=True)
class TaskState:
    """Tracks the state of an in-progress or completed background task."""

    task_id: str
    kind: str  # "sync", "ingest", "remember", "delete", "register", "deregister"
    status: str = "running"
    results: dict[str, object] = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.monotonic)


@contextmanager
def task_terminal(state: TaskState) -> Generator[None]:
    """Record *state*'s terminal status when its background body exits.

    Cancellation is recorded then re-raised so the event loop still observes
    it; any other exception is logged and recorded as the failure reason; a
    body that exits without setting a terminal status is marked failed so no
    task is ever left stuck in ``running`` (a guard for future code paths).
    """
    try:
        yield
    except asyncio.CancelledError:
        state.status = "failed"
        state.error = "task was cancelled"
        raise
    except Exception as exc:
        logger.exception("Background %s failed", state.kind)
        state.status = "failed"
        state.error = str(exc)
    finally:
        if state.status == "running":
            state.status = "failed"
            state.error = "task exited without setting terminal status"


@final
class TaskRegistry:
    """Owns the daemon's in-flight and completed background-task states.

    The registry is the single home for both the pollable ``TaskState`` records
    and the live ``asyncio.Task`` handles that drive them, so a handler never
    reaches into raw dicts to bookkeep a task.
    """

    __slots__ = ("_refs", "_states")

    _states: dict[str, TaskState]
    _refs: dict[str, asyncio.Task[None]]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._states = {}
        self._refs = {}
        return self

    def begin(self, kind: str) -> TaskState:
        """Create a fresh ``TaskState``, evicting states older than the TTL."""
        now = time.monotonic()
        expired = [
            tid
            for tid, task in self._states.items()
            if task.status != "running" and (now - task.created_at) > TASK_TTL_SECONDS
        ]
        for tid in expired:
            del self._states[tid]
            self._refs.pop(tid, None)
        task_id = f"{kind}-{uuid.uuid4().hex[:12]}"
        state = TaskState(task_id=task_id, kind=kind)
        self._states[task_id] = state
        return state

    def track(self, state: TaskState, task: asyncio.Task[None]) -> None:
        """Retain *task* until it reaches a terminal state, then drop the ref."""
        tid = state.task_id
        self._refs[tid] = task
        task.add_done_callback(lambda _t: self._refs.pop(tid, None))

    def seed(self, state: TaskState) -> None:
        """Insert a pre-built *state* keyed by its ``task_id``.

        Used to bootstrap a task whose id is chosen by the caller (tests seed
        states with fixed ids to exercise GC and concurrency paths).
        """
        self._states[state.task_id] = state

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._states

    def __len__(self) -> int:
        return len(self._states)

    def get(self, task_id: str) -> TaskState | None:
        """Return the state for *task_id*, or ``None`` if unknown.

        Absence is the documented contract — the task route maps ``None`` to a
        404, so a missing id is an expected outcome, not an error.
        """
        return self._states.get(task_id)

    def running_of_kind(self, kind: str) -> TaskState | None:
        """Return a still-running task of *kind*, or ``None`` if none is active.

        ``None`` is the documented contract — the sync route uses it to decide
        whether to reject a concurrent request with 409.
        """
        return next(
            (
                t
                for t in self._states.values()
                if t.kind == kind and t.status == "running"
            ),
            None,
        )
