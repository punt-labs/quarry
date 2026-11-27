"""Rate-limit the per-database finalize (table optimize + full FTS rebuild).

The finalize is the heavy tail of every watch batch, and it is table-wide: it
compacts one database's chunks table and rebuilds the whole FTS index, every
collection's rows included.  Under sustained churn the debouncer would flush one
per window, so the daemon would compact continuously; this coalesces them.

Lives apart from :mod:`quarry.daemon.watch_submit` because it is a rate-limiting
policy with no knowledge of jobs, route keys, or the queue — its collaborator is
a thunk — while the submitter's job is turning watch events into IngestUnits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable


@final
class FinalizeThrottle:
    """Rate-limit the per-database finalize (optimize + full FTS rebuild).

    A finalize is the heavy tail of every watch batch, so under sustained churn
    the debouncer flushes one every window and the daemon compacts continuously.
    This coalesces them per database: the first request runs immediately, any
    request inside the interval arms a single trailing timer that runs one
    finalize when the interval elapses — so continuous churn costs one finalize
    per interval, and a finalize always lands after churn settles.  All state
    lives on the event loop, so no lock is needed.

    Granularity is the DATABASE, not the collection.  The trailing timer is armed
    on the first requesting collection's routing key, so it may run on that
    collection's FIFO before a *different* collection's in-flight file-index jobs
    complete.  The rebuild is table-wide, so it still reindexes every collection's
    already-committed chunks; only files a collection indexes *after* this
    finalize lag in FTS.  That lag self-heals within one bounded window: the
    periodic reconcile (``watch_safety_scan_s``, default 300 s) re-scans every
    registered collection with an IMMEDIATE (un-throttled) finalize.  The vector
    channel is never stale — files index immediately — so hybrid search degrades
    to vector-only for the lagging collection until the next finalize, never
    silently wrong.  With ``watch_safety_scan_s = 0`` the reconcile backstop is
    off and FTS lag clears only on that collection's next churn.

    The reconcile's finalize is submitted immediately, but it rewrites the table
    only if the table changed since its last successful finalize, so it is a
    backstop for a dirty table, not an unconditional self-heal.  The throttle
    stamps its clock when it ADMITS a finalize, before the job decides to skip: a
    throttled finalize that finds the table clean still pushes the next request
    inside the interval into the trailing timer, delaying a real rewrite by at
    most one interval.  A sweep's finalize stamps only when it rewrites
    (``mark_admitted``, called from the job).  A finalize that leaves its table
    dirty is retried by ``FinalizeRetry``.
    """

    __slots__ = ("_interval", "_last", "_loop", "_timers")

    _loop: asyncio.AbstractEventLoop
    _interval: float
    _last: dict[str, float]
    _timers: dict[str, asyncio.TimerHandle]

    def __new__(cls, loop: asyncio.AbstractEventLoop, interval_s: float) -> Self:
        """Start an empty per-database finalize schedule.

        Args:
            interval_s: Minimum seconds between finalizes for one database.
        """
        self = super().__new__(cls)
        self._loop = loop
        self._interval = interval_s
        self._last = {}
        self._timers = {}
        return self

    def request(self, database: str, submit: Callable[[], bool]) -> None:
        """Run *submit* now, or coalesce it into *database*'s trailing timer.

        ``submit`` is a no-argument thunk returning whether the finalize was
        ADMITTED (the DES-042 queue can shed it when full), so the throttle stays
        ignorant of jobs and route keys.  A disabled interval (<= 0) runs every
        request inline.
        """
        if self._interval <= 0:
            submit()
            return
        now = self._loop.time()
        last = self._last.get(database)
        if last is None or now - last >= self._interval:
            self._fire(database, submit)
            return
        if database not in self._timers:
            delay = last + self._interval - now
            self._timers[database] = self._loop.call_later(
                delay, self._fire, database, submit
            )

    def cancel_all(self) -> None:
        """Cancel every pending trailing finalize (loop shutdown)."""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()

    def mark_admitted(self, database: str) -> None:
        """Record an admitted finalize and cancel any pending trailing timer.

        Callers that finalize a database OUTSIDE this throttle — the reconcile and
        explicit-sync scans, which run an immediate finalize directly — call this
        so their finalize and an already-armed trailing finalize for the same
        database do not both run (a redundant double-compaction).  Cancelling,
        not just dropping, de-queues an overdue timer callback; stamping ``_last``
        makes the next in-window request coalesce behind the finalize that ran.

        Call this ONLY when the finalize was admitted — a shed one must not stamp
        (it never ran) and must leave any pending trailing timer intact to retry.
        """
        self._consume_timer(database)
        self._last[database] = self._loop.time()

    def _fire(self, database: str, submit: Callable[[], bool]) -> None:
        """Finalize *database* now: run submit, then stamp only if it was admitted.

        Consume any pending trailing timer first: the immediate path fires exactly
        when a trailing timer is due (both at ``last + interval``), so a debounce
        flush in that tick would otherwise run this finalize AND leave the overdue
        timer queued — two finalizes for one interval.

        Stamp the clock only on ADMISSION.  A shed submit (queue full) never ran a
        finalize, so stamping would push the next attempt out a whole interval;
        leaving ``_last`` unstamped lets the next ``request`` retry immediately
        (the reconcile at ``watch_safety_scan_s`` is the eventual backstop).
        """
        self._consume_timer(database)
        if submit():
            self._last[database] = self._loop.time()

    def _consume_timer(self, database: str) -> None:
        """Cancel and drop any pending or just-fired trailing timer for *database*."""
        timer = self._timers.pop(database, None)
        if timer is not None:
            timer.cancel()
