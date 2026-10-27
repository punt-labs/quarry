"""Wait for a slot on a collection's FIFO: backing off, never past a deadline."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.ingest_refusal import IngestRefusal, IngestRefusedError

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.route_key import RouteKey
    from quarry.daemon.tasks import TaskState

_FIRST_BACKOFF_S = 0.05
_MAX_BACKOFF_S = 1.0


@final
class JobAdmission:
    """A patient producer's wait for room: *timeout_s* long, leaving *reserve* free.

    The wait ends at once when the daemon is shutting down, and at the deadline
    otherwise, each in a refusal that says which.  *reserve* slots are kept free
    so a producer that can wait never makes a hook's capture shed.
    """

    __slots__ = ("_ctx", "_reserve", "_timeout_s")

    _ctx: DaemonContext
    _reserve: int
    _timeout_s: float

    def __new__(cls, ctx: DaemonContext, timeout_s: float, reserve: int) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        self._timeout_s = timeout_s
        self._reserve = reserve
        return self

    async def take(self, key: RouteKey, job: IngestUnit, state: TaskState) -> None:
        """Wait for a slot on *key*'s FIFO and enqueue *job* there."""
        queue = self._ctx.ingest_queue
        gate = queue.gate
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_s
        delay = _FIRST_BACKOFF_S
        # One synchronous step (no await between the look and the take), so a
        # hook cannot slip into the headroom this producer was told it had.
        while not (gate.has_room(self._reserve) and queue.try_submit(key, job, state)):
            if gate.closing:
                msg = "daemon shutting down"
                raise IngestRefusedError.because(IngestRefusal.SHUTTING_DOWN, msg)
            remaining = deadline - loop.time()
            if remaining <= 0:
                msg = "ingest queue full; not admitted in time"
                raise IngestRefusedError.because(IngestRefusal.NOT_ADMITTED, msg)
            await gate.wait_released(min(delay, remaining))
            delay = min(delay * 2, _MAX_BACKOFF_S)
