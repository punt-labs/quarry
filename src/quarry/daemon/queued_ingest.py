"""Run one job through its collection's FIFO writer and wait for how it ended."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.ingest_refusal import IngestRefusal, IngestRefusedError
from quarry.daemon.job_admission import JobAdmission
from quarry.daemon.job_claim import JobClaim
from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import TaskState

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit

_POLL_S = 0.05
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

    __slots__ = ("_admission", "_ctx", "_kind", "_policy")

    _admission: JobAdmission
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
        self._admission = JobAdmission(ctx, policy.admit_timeout_s, policy.reserve)
        return self

    def new_state(self) -> TaskState:
        """Return the private state a job submitted here reports through."""
        return TaskState(
            task_id=f"{self._kind}-{uuid.uuid4().hex[:12]}",
            kind=self._kind,
            status="new",
        )

    async def run(self, job: IngestUnit) -> TaskState:
        """Return *job*'s terminal state once its worker has finished it."""
        return await self.run_tracked(job, self.new_state())

    async def run_tracked(self, job: IngestUnit, state: TaskState) -> TaskState:
        """Return *job*'s terminal state, reporting through *state*.

        The caller keeps *state* (from :meth:`new_state`), so a producer that
        stops waiting can withdraw the job and learn whether it had started.
        A job still waiting when this wait ends for any reason is withdrawn: its
        worker skips it, so nothing the producer gave up on is written later.

        Raises:
            IngestRefusedError: The daemon is shutting down, the queue had no
                room within the wait, the worker died, or the job did not finish
                within the deadline; its ``reason`` says which.
        """
        key = RouteKey(self._ctx.database_name, job.collection)
        try:
            await self._admission.take(key, job, state)
            await self._finish(key, state)
        except (asyncio.CancelledError, IngestRefusedError):
            JobClaim(state).withdraw()
            raise
        return state

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
