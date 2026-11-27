"""Hermetic tests for FinalizeThrottle — per-database finalize rate-limiting.

Driven on a real event loop with a tiny interval and a callable that records
calls and reports admission, so the run-now / coalesce-into-trailing /
per-database-independence / shed-does-not-stamp behaviours are asserted without
the queue, watchdog, or the filesystem.
"""

from __future__ import annotations

import asyncio
from typing import Self, final

from quarry.daemon.finalize_throttle import FinalizeThrottle

_INTERVAL = 0.05


@final
class _Submit:
    """A finalize-submit stand-in: counts invocations, reports admitted or shed."""

    __slots__ = ("_admitted", "_calls")

    _calls: int
    _admitted: bool

    def __new__(cls, *, admitted: bool = True) -> Self:
        self = super().__new__(cls)
        self._calls = 0
        self._admitted = admitted
        return self

    def __call__(self) -> bool:
        self._calls += 1
        return self._admitted

    @property
    def calls(self) -> int:
        """Return how many times the throttle invoked this submit."""
        return self._calls


def test_first_request_runs_immediately() -> None:
    async def _run() -> None:
        submit = _Submit()
        throttle = FinalizeThrottle(asyncio.get_running_loop(), _INTERVAL)
        throttle.request("db", submit)
        assert submit.calls == 1

    asyncio.run(_run())


def test_burst_within_interval_coalesces_to_one_trailing() -> None:
    """First fires now; the rest inside the window collapse to one trailing run."""

    async def _run() -> None:
        submit = _Submit()
        throttle = FinalizeThrottle(asyncio.get_running_loop(), _INTERVAL)
        for _ in range(10):
            throttle.request("db", submit)
        assert submit.calls == 1  # only the first ran inline
        await asyncio.sleep(_INTERVAL * 2)
        assert submit.calls == 2  # exactly one trailing finalize, not ten

    asyncio.run(_run())


def test_request_after_interval_runs_immediately_again() -> None:
    async def _run() -> None:
        submit = _Submit()
        throttle = FinalizeThrottle(asyncio.get_running_loop(), _INTERVAL)
        throttle.request("db", submit)
        await asyncio.sleep(_INTERVAL * 2)
        throttle.request("db", submit)
        assert submit.calls == 2  # interval elapsed, so the second ran inline

    asyncio.run(_run())


def test_databases_are_independent() -> None:
    async def _run() -> None:
        a, b = _Submit(), _Submit()
        throttle = FinalizeThrottle(asyncio.get_running_loop(), _INTERVAL)
        throttle.request("a", a)
        throttle.request("b", b)
        # Each database's first request runs immediately, independent of the other.
        assert (a.calls, b.calls) == (1, 1)

    asyncio.run(_run())


def test_disabled_interval_runs_every_request() -> None:
    async def _run() -> None:
        submit = _Submit()
        throttle = FinalizeThrottle(asyncio.get_running_loop(), 0.0)
        for _ in range(5):
            throttle.request("db", submit)
        assert submit.calls == 5  # rate-limit off: no coalescing

    asyncio.run(_run())


def test_cancel_all_drops_pending_trailing() -> None:
    async def _run() -> None:
        submit = _Submit()
        throttle = FinalizeThrottle(asyncio.get_running_loop(), _INTERVAL)
        throttle.request("db", submit)  # runs now
        throttle.request("db", submit)  # arms a trailing timer
        throttle.cancel_all()
        await asyncio.sleep(_INTERVAL * 2)
        assert submit.calls == 1  # the trailing finalize was cancelled

    asyncio.run(_run())


def test_immediate_path_cancels_pending_timer_no_double_fire() -> None:
    """The immediate path must CANCEL a pending trailing timer, not just drop it.

    Otherwise a debounce flush landing in the tick the timer is due runs the
    immediate finalize AND leaves the overdue timer queued → two finalizes.
    """

    async def _run() -> None:
        submit = _Submit()
        loop = asyncio.get_running_loop()
        # Long interval so the trailing timer stays pending under our control.
        throttle = FinalizeThrottle(loop, 100.0)
        throttle.request("db", submit)  # fires now (calls=1), stamps _last
        throttle.request("db", submit)  # within interval → arms a trailing timer
        # White-box: reach into the throttle's event-loop state to force the exact
        # race deterministically (real timing would be flaky).
        pending = throttle._timers["db"]
        assert not pending.cancelled()
        # Force the immediate branch while the timer is still pending, exactly as a
        # debounce flush arriving at/after the due time would: backdate _last.
        throttle._last["db"] = loop.time() - 200.0
        throttle.request("db", submit)  # immediate _fire → must cancel `pending`
        assert submit.calls == 2  # the immediate finalize ran once more
        assert pending.cancelled()  # THE FIX: no overdue timer left to double-fire
        await asyncio.sleep(0)  # let any (wrongly) surviving callback run
        assert submit.calls == 2

    asyncio.run(_run())


def test_mark_admitted_stamps_but_leaves_a_pending_trailing_armed() -> None:
    """A rewriting sweep finalize stamps the clock; it must not cancel a trailing
    timer, which a write queued behind that finalize may have armed.
    """

    async def _run() -> None:
        submit = _Submit()
        loop = asyncio.get_running_loop()
        throttle = FinalizeThrottle(loop, _INTERVAL)
        throttle.request("db", submit)  # fires now (calls=1), stamps _last
        throttle.request("db", submit)  # within interval → arms a trailing timer
        pending = throttle._timers["db"]
        before = throttle._last["db"]
        await asyncio.sleep(0.01)
        throttle.mark_admitted("db")
        assert not pending.cancelled()
        assert throttle._timers["db"] is pending
        assert throttle._last["db"] > before
        await asyncio.sleep(_INTERVAL * 2)  # the trailing fires: it skips when clean
        assert submit.calls == 2

    asyncio.run(_run())


def test_shed_finalize_does_not_stamp_next_request_retries() -> None:
    """A shed submit (queue full) must NOT stamp the clock, so a subsequent
    request within the interval finalizes immediately instead of waiting one
    full interval for a finalize that never ran.
    """

    async def _run() -> None:
        shed = _Submit(admitted=False)
        loop = asyncio.get_running_loop()
        throttle = FinalizeThrottle(
            loop, 100.0
        )  # long interval: only a stamp coalesces
        throttle.request("db", shed)  # _fire, submit sheds → must not stamp
        assert shed.calls == 1
        assert "db" not in throttle._last  # unstamped: the finalize never ran
        # A subsequent request within the (long) interval retries immediately,
        # because the shed one left the clock unstamped.
        admitted = _Submit()
        throttle.request("db", admitted)
        assert admitted.calls == 1  # ran now, not coalesced behind a phantom stamp

    asyncio.run(_run())


def test_admitted_finalize_stamps_and_coalesces() -> None:
    """The mirror of the shed case: an admitted finalize stamps, so a subsequent
    in-interval request coalesces into a trailing timer instead of running now.
    """

    async def _run() -> None:
        submit = _Submit()
        loop = asyncio.get_running_loop()
        throttle = FinalizeThrottle(loop, 100.0)
        throttle.request("db", submit)  # admitted → stamps _last
        assert "db" in throttle._last
        follow = _Submit()
        throttle.request("db", follow)  # within interval → coalesced, not run now
        assert follow.calls == 0
        assert "db" in throttle._timers

    asyncio.run(_run())
