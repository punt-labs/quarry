"""Run one job through its collection's FIFO writer and wait for how it ended."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.ingest_refusal import IngestRefusal, IngestRefusedError
from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import TaskState

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit

_POLL_S = 0.05
_FIRST_BACKOFF_S = 0.05
_MAX_BACKOFF_S = 1.0
_TERMINAL = frozenset({"completed", "failed"})


@final
@dataclass(frozen=True, slots=True)
class QueuePolicy:
    """How long a patient producer waits, and how much room it leaves for hooks.

    *admit_timeout_s* bounds the wait for a slot.  *finish_timeout_s* bounds the
    wait for a terminal state: one transcript's chunk, embed and write takes
    seconds, minutes at the very most on CPU, so thirty minutes fires only on a
    hang, never on a slow job.  *grace_s* is the margin a producer on another
    thread adds when it waits for the result.  *reserve* slots are kept free so a
    producer that can wait never makes a hook's capture shed.
    """

    admit_timeout_s: float = 60.0
    finish_timeout_s: float = 1800.0
    grace_s: float = 10.0
    reserve: int = 8

    @property
    def total_s(self) -> float:
        """Return the longest a whole submit-and-wait may take."""
        return self.admit_timeout_s + self.finish_timeout_s + self.grace_s


DEFAULT_QUEUE_POLICY = QueuePolicy()


@final
class QueuedIngest:
    """Submit a job to its collection's worker and wait for its terminal state.

    The task registry is for work a client polls, so a job submitted here gets a
    private :class:`TaskState` of the given *kind* and no registry entry: a
    backfill of a thousand transcripts is one registered task, not a thousand.
    Neither wait is unbounded.  The admission wait ends at the policy's deadline,
    or at once when the daemon is shutting down.  The completion wait ends when
    the worker is no longer alive or the completion deadline passes.  Each ends
    in a refusal that says which, never a drop and never a hang.
    """

    __slots__ = ("_ctx", "_kind", "_policy")

    _ctx: DaemonContext
    _kind: str
    _policy: QueuePolicy

    def __new__(
        cls,
        ctx: DaemonContext,
        kind: str,
        policy: QueuePolicy = DEFAULT_QUEUE_POLICY,
    ) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        self._kind = kind
        self._policy = policy
        return self

    async def run(self, job: IngestUnit) -> TaskState:
        """Return *job*'s terminal state once its worker has finished it.

        Raises:
            IngestRefusedError: The daemon is shutting down, the queue had no
                room within the wait, the worker died, or the job did not finish
                within the deadline; its ``reason`` says which.
        """
        state = TaskState(
            task_id=f"{self._kind}-{uuid.uuid4().hex[:12]}", kind=self._kind
        )
        key = RouteKey(self._ctx.database_name, job.collection)
        await self._admit(key, job, state)
        await self._finish(key, state)
        return state

    async def _admit(self, key: RouteKey, job: IngestUnit, state: TaskState) -> None:
        """Wait for a slot, backing off, but never past the deadline or a shutdown."""
        queue = self._ctx.ingest_queue
        gate = queue.gate
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._policy.admit_timeout_s
        delay = _FIRST_BACKOFF_S
        # One synchronous step (no await between the look and the take), so a
        # hook cannot slip into the headroom this producer was told it had.
        while not (
            gate.has_room(self._policy.reserve) and queue.try_submit(key, job, state)
        ):
            if gate.closing:
                msg = "daemon shutting down"
                raise IngestRefusedError.because(IngestRefusal.SHUTTING_DOWN, msg)
            remaining = deadline - loop.time()
            if remaining <= 0:
                msg = "ingest queue full; not admitted in time"
                raise IngestRefusedError.because(IngestRefusal.NOT_ADMITTED, msg)
            await gate.wait_released(min(delay, remaining))
            delay = min(delay * 2, _MAX_BACKOFF_S)

    async def _finish(self, key: RouteKey, state: TaskState) -> None:
        """Wait for a terminal state while the worker lives and the deadline holds."""
        queue = self._ctx.ingest_queue
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._policy.finish_timeout_s
        while state.status not in _TERMINAL:
            if not queue.pool.is_alive(key):
                msg = "collection worker stopped"
                raise IngestRefusedError.because(IngestRefusal.WORKER_STOPPED, msg)
            if loop.time() >= deadline:
                msg = f"ingest did not finish within {self._policy.finish_timeout_s:g}s"
                raise IngestRefusedError.because(IngestRefusal.UNFINISHED, msg)
            await asyncio.sleep(_POLL_S)
