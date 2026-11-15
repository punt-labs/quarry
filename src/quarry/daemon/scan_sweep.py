"""Rescan many collections while finalizing each database exactly once.

A finalize compacts one database's chunks table and rebuilds its whole FTS
index — every collection's rows, in one pass.  Submitting one per collection
therefore repeats identical table-wide work: six registered collections meant
six back-to-back optimize+rebuild pairs on every safety scan, which is what the
daemon's log showed.  The right granularity is the DATABASE, which is what
:class:`~quarry.daemon.finalize_throttle.FinalizeThrottle` already assumes for
the churn path; this applies it to the sweeps.

The finalize stays IMMEDIATE rather than throttled.  Every sweep is one-shot or
the backstop — the initial watch scan, an explicit ``quarry sync``, the periodic
reconcile whose finalize is the FTS self-heal — so it must always run, never
coalesce away.  Deduplication and rate-limiting are different mechanisms with
different consequences: one drops repeated work, the other defers needed work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.daemon.route_key import RouteKey
    from quarry.daemon.task_state import TaskState
    from quarry.daemon.watch_submit import WatchSubmitter


@final
class ScanSweep:
    """Accumulate per-collection scans, then finalize each database once.

    Built by :meth:`~quarry.daemon.watch_submit.WatchSubmitter.sweep`.  A sweep
    is single-use: :meth:`finish` submits the deferred finalizes and reports
    every task state the sweep produced.
    """

    __slots__ = ("_collections", "_databases", "_states", "_submitter")

    _submitter: WatchSubmitter
    _states: list[TaskState]
    _databases: dict[str, RouteKey]
    _collections: int

    def __new__(cls, submitter: WatchSubmitter) -> Self:
        self = super().__new__(cls)
        self._submitter = submitter
        self._states = []
        self._databases = {}
        self._collections = 0
        return self

    @property
    def collections(self) -> int:
        """Return how many collections this sweep scanned."""
        return self._collections

    def add(self, key: RouteKey, root: Path) -> None:
        """Submit *key*'s bulk rescan now and mark its database for finalizing.

        The first collection seen for a database owns that database's finalize.
        The finalize is table-wide, so which collection's routing key carries it
        does not affect what it does — only which FIFO it queues behind.
        """
        self._states.append(self._submitter.submit_scan_job(key, root))
        self._collections += 1
        self._databases.setdefault(key.database, key)

    def finish(self) -> list[TaskState]:
        """Submit one finalize per database touched; return every task state.

        Each finalize registers with the throttle on admission so it cancels a
        trailing finalize already armed for that database and resets the
        interval clock — otherwise this sweep's finalize and the armed one would
        both run, the redundant double-compaction the throttle exists to avoid.
        """
        for database, key in self._databases.items():
            state = self._submitter.submit_finalize_job(key)
            if state.status != "failed":
                self._submitter.mark_finalized(database)
            self._states.append(state)
        self._databases.clear()
        return self._states
