"""Background-task bookkeeping for the daemon: state and GC.

Every long-running REST operation (sync, ingest, remember, delete, register,
deregister) runs as an asyncio task and reports progress through a ``TaskState``
that the client polls via ``GET /tasks/{task_id}``.  ``TaskRegistry`` owns
the live and completed states plus their asyncio task handles, evicting
completed states after a TTL so a long-lived daemon does not leak them.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal, Self, final

# Task GC: completed/failed tasks are evicted after this many seconds.
TASK_TTL_SECONDS = 3600  # 1 hour

# The one status vocabulary of a background task and of a queued job.  ``new``,
# ``queued``, ``running``, ``committing``, ``aborted`` and ``withdrawn`` belong to
# a queued job's life (:class:`~quarry.daemon.job_lifecycle.JobLifecycle`); a
# polled task is ``running`` until it is ``completed`` or ``failed``.
TaskStatus = Literal[
    "new",
    "queued",
    "running",
    "committing",
    "aborted",
    "withdrawn",
    "completed",
    "failed",
]

# Terminal statuses, eligible for GC.  A task in any other status is still live
# and must never be evicted out from under a client polling it, and a job whose
# body exits in one of them did not finish.
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset({"completed", "failed"})


@dataclass(slots=True)
class TaskState:
    """Tracks the state of an in-progress or completed background task.

    ``holds_thread`` is set while a scan thread this task started is alive: the
    singleton guard counts the task as running until the thread has returned,
    whatever ``status`` says.  ``claim`` is the lock under which a job is run
    or withdrawn, never both (:class:`~quarry.daemon.job_lifecycle.JobLifecycle`).
    """

    task_id: str
    kind: str  # "sync", "ingest", "remember", "delete", "register", "deregister"
    status: TaskStatus = "running"
    results: dict[str, object] = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.monotonic)
    holds_thread: bool = False
    claim: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def reason_or(self, default: str) -> str:
        """Return the reason the task has already given, else *default*."""
        return self.error or default


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
        self._evict_expired()
        task_id = f"{kind}-{uuid.uuid4().hex[:12]}"
        state = TaskState(task_id=task_id, kind=kind)
        self._states[task_id] = state
        return state

    def _evict_expired(self) -> None:
        """Drop every terminal state (and its task ref) older than the TTL."""
        now = time.monotonic()
        expired = [
            tid
            for tid, task in self._states.items()
            if self._is_idle(task) and (now - task.created_at) > TASK_TTL_SECONDS
        ]
        for tid in expired:
            del self._states[tid]
            self._refs.pop(tid, None)

    @staticmethod
    def _is_idle(task: TaskState) -> bool:
        """Return whether *task* ended and nothing of it, a scan thread, still runs."""
        return task.status in TERMINAL_STATUSES and not task.holds_thread

    def drop(self, state: TaskState) -> None:
        """Discard a task record so a rejected submit leaves no orphan behind."""
        self._states.pop(state.task_id, None)
        self._refs.pop(state.task_id, None)

    def track(self, state: TaskState, task: asyncio.Task[None]) -> None:
        """Retain *task* until it reaches a terminal state, then drop the ref."""
        tid = state.task_id
        self._refs[tid] = task
        task.add_done_callback(lambda _t: self._refs.pop(tid, None))

    def tracked(self) -> list[asyncio.Task[None]]:
        """Return the live task handles, for a teardown to await."""
        return list(self._refs.values())

    def holding_threads(self) -> list[TaskState]:
        """Return the tasks whose scan thread is still alive, finished or not."""
        return [state for state in self._states.values() if state.holds_thread]

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

        A task whose scan thread is still alive is running, however it ended.

        ``None`` is the documented contract — the sync route uses it to decide
        whether to reject a concurrent request with 409.
        """
        return next(
            (
                t
                for t in self._states.values()
                if t.kind == kind and (t.status == "running" or t.holds_thread)
            ),
            None,
        )
