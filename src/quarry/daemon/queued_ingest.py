"""Run one job through its collection's FIFO writer and wait for how it ended."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import TaskState
from quarry.refusal import RefusalError

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit

_POLL_S = 0.05
_TERMINAL = frozenset({"completed", "failed"})


class IngestNotAdmittedError(RefusalError):
    """The ingest queue stayed full for the whole wait: the job never ran."""


@final
class QueuedIngest:
    """Submit a job to its collection's worker and wait for its terminal state.

    The task registry is for work a client polls, so a job submitted here gets a
    private :class:`TaskState` and no registry entry: a backfill of a thousand
    transcripts is one registered task, not a thousand.  A full queue is waited
    out for *admit_timeout_s*; past it the job is refused, never dropped silently
    and never waited on forever.
    """

    __slots__ = ("_admit_timeout_s", "_ctx")

    _ctx: DaemonContext
    _admit_timeout_s: float

    def __new__(cls, ctx: DaemonContext, admit_timeout_s: float = 60.0) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        self._admit_timeout_s = admit_timeout_s
        return self

    async def run(self, job: IngestUnit) -> TaskState:
        """Return *job*'s terminal state once its worker has finished it.

        Raises:
            IngestNotAdmittedError: The queue had no room within the wait.
        """
        state = TaskState(task_id=f"queued-{uuid.uuid4().hex[:12]}", kind="queued")
        key = RouteKey(self._ctx.database_name, job.collection)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._admit_timeout_s
        while not self._ctx.ingest_queue.try_submit(key, job, state):
            if loop.time() >= deadline:
                msg = "ingest queue full; not admitted in time"
                raise IngestNotAdmittedError(msg)
            await asyncio.sleep(_POLL_S)
        while state.status not in _TERMINAL:
            await asyncio.sleep(_POLL_S)
        return state
