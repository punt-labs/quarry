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
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self, final

from quarry.failure_reason import FailureReason

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

# Task GC: completed/failed tasks are evicted after this many seconds.
TASK_TTL_SECONDS = 3600  # 1 hour

# Terminal statuses eligible for GC.  A "queued" or "running" task is still live
# and must never be evicted out from under a client polling it (DES-042 adds the
# interim "queued" state that a worker flips to "running" on dequeue).
_TERMINAL_STATUSES = frozenset({"completed", "failed"})


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

    Cancellation is recorded and re-raised; any other exception is logged and recorded
    as its own text when it is a refusal or a ``ReportedError``, else as ``internal
    error: <Class>``; a body that sets no terminal status is marked failed.
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
        state.error = FailureReason.of(exc)
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
            if task.status in _TERMINAL_STATUSES
            and (now - task.created_at) > TASK_TTL_SECONDS
        ]
        for tid in expired:
            del self._states[tid]
            self._refs.pop(tid, None)

    def drop(self, state: TaskState) -> None:
        """Discard a task record so a rejected submit leaves no orphan behind."""
        self._states.pop(state.task_id, None)
        self._refs.pop(state.task_id, None)

    def track(self, state: TaskState, task: asyncio.Task[None]) -> None:
        """Retain *task* until it reaches a terminal state, then drop the ref."""
        tid = state.task_id
        self._refs[tid] = task
        task.add_done_callback(lambda _t: self._refs.pop(tid, None))

    async def drain(self, timeout: float) -> None:
        """Await every tracked task's real completion within *timeout*.

        Cancelling the wrapping ``asyncio.Task`` does not stop a
        ``run_in_threadpool`` job's underlying OS thread — Python has no way to
        interrupt a running thread — so a cancel-only teardown lets that thread
        keep executing (a real ingest, a real network fetch, a real log call)
        well after the caller believes the task is gone.  A daemon/test-harness
        teardown that only cancels can therefore race: the thread finishes later,
        inside a *different* context (a later test's mocks, a closed log stream).
        Waiting for genuine completion is the only way to close that race.

        A task that outlives the drain window is a genuine straggler: its
        ``asyncio.Task`` wrapper is best-effort cancelled (to release the
        reference and unblock event-loop bookkeeping), but — per the paragraph
        above — that does **not** stop the underlying thread, so the race is
        NOT closed for a straggler.  Pretending otherwise (swallowing the
        timeout and returning normally) would silently reintroduce the exact
        bug this method exists to fix.  So this fails closed: log the straggler
        and re-raise ``TimeoutError`` rather than let the caller believe drain
        succeeded. Must be called on the registry's own event-loop thread.
        """
        tasks = list(self._refs.values())
        if not tasks:
            return
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            await self._cancel_stragglers(tasks)
            logger.error(
                "TaskRegistry.drain: %d task(s) did not complete within %.1fs; "
                "their wrapper was cancelled but the underlying thread may still "
                "be running",
                len(tasks),
                timeout,
            )
            raise

    async def _cancel_stragglers(self, tasks: list[asyncio.Task[None]]) -> None:
        """Best-effort cancel *tasks* that outlived the drain window, then await them.

        Only releases each wrapper's ``asyncio.Task`` reference — it does NOT
        stop the underlying ``run_in_threadpool`` OS thread (see :meth:`drain`).
        """
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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
