"""Periodic open-file-descriptor telemetry for the ``quarryd`` daemon.

The long-lived daemon can leak descriptors: handles to deleted LanceDB index
files accumulate until the count reaches ``RLIMIT_NOFILE``, the kernel returns
EMFILE, and requests start failing with HTTP 500. Sampling the count on a fixed
cadence makes a climbing trend visible in the logs long before exhaustion. The
leak fix lives elsewhere (``db/connection.py``); this is the observability side.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Self, final

from quarry.fd_headroom import FdHeadroom

logger = logging.getLogger(__name__)


@final
class FdTelemetry:
    """Fixed-cadence open-fd sampling that outlives a failing sample.

    Wraps an :class:`FdHeadroom` reading in a loop that logs each sample and —
    the point of the class — survives a sample that *raises*, so telemetry keeps
    running through the EMFILE window it exists to make visible.
    """

    __slots__ = ("_interval",)

    _interval: float

    def __new__(cls, interval: float) -> Self:
        self = super().__new__(cls)
        self._interval = interval
        return self

    @staticmethod
    def sample() -> None:
        """Sample and log the daemon's open file-descriptor usage once.

        Delegates the measurement to :class:`FdHeadroom`, which scans
        ``/proc/self/fd`` (falling back to ``/dev/fd``) against the soft
        ``RLIMIT_NOFILE``. Logged at WARNING once usage crosses the warning ratio
        so a climbing count surfaces before it reaches EMFILE, at INFO otherwise.
        Utilization is zero when the soft limit is unbounded, so an unlimited
        limit never trips the warning. Propagates ``OSError(EMFILE)`` when
        exhaustion prevents the scan — :meth:`run` turns that into a single
        logged line and keeps sampling rather than dying.
        """
        headroom = FdHeadroom.sample()
        pct_used = round(headroom.utilization * 100.0, 1)
        level = logging.WARNING if headroom.is_low else logging.INFO
        logger.log(
            level,
            "fd_usage open_fds=%d soft_limit=%d pct_used=%.1f",
            headroom.open_fds,
            headroom.soft_limit,
            pct_used,
            extra={
                "open_fds": headroom.open_fds,
                "fd_soft_limit": headroom.soft_limit,
                "fd_pct_used": pct_used,
            },
        )

    async def run(self) -> None:
        """Sample every ``interval`` seconds until cancelled.

        A sample that raises — EMFILE mid-scan, or a container with no fd
        directory — must never end telemetry for the daemon's remaining life:
        log once (with the traceback) and keep ticking so the next interval can
        recover. This is a long-lived event-loop boundary, so the broad catch is
        deliberate; ``CancelledError`` is a ``BaseException``, so shutdown
        cancellation still breaks the loop cleanly past this guard.
        """
        while True:
            await asyncio.sleep(self._interval)
            try:
                self.sample()
            except Exception:
                logger.exception("fd_usage sample failed")
