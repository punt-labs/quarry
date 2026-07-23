"""Per-path debounce and bulk coalescing for the watch loop (DES-045 §5).

An editor writes a file ten times in two seconds; the daemon wants ONE reindex,
of the final bytes.  :class:`DebouncedDispatcher` keys pending changes by
``(RouteKey, path)`` and, per route key, (re)arms a single timer at
``debounce_s`` — bounded by ``max_delay_s`` so a continuously-rearmed hot file
still indexes (anti-starvation).  When the quiet window elapses it emits one
:class:`FlushBatch`: either per-file modified/deleted paths, or — when more than
``bulk_threshold`` distinct paths accumulated — a single bulk marker so a branch
switch becomes one scan job, not thousands of admissions.

The dispatcher lives on the daemon's event loop; the watch loop marshals every
observer-thread event onto that loop before calling :meth:`feed`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable
    from pathlib import Path

    from quarry.daemon.fs_events import FsEvent
    from quarry.daemon.route_key import RouteKey

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class FlushBatch:
    """One route key's coalesced changes at the end of a debounce window.

    ``bulk`` set means the pending set exceeded ``bulk_threshold`` — the loop
    submits one ``CollectionSyncJob`` and ignores ``modified``/``deleted`` (both
    empty).  Otherwise ``modified`` paths route to ``FileIndexJob``s and
    ``deleted`` paths to ``DocumentDeleteJob``s.
    """

    key: RouteKey
    modified: tuple[Path, ...]
    deleted: tuple[Path, ...]
    bulk: bool


@final
class _PendingDir:
    """One route key's accumulating change set and its single pending timer."""

    __slots__ = ("_first", "_paths", "_timer")

    _first: float
    _paths: dict[Path, bool]  # path -> was the last event a deletion?
    _timer: asyncio.TimerHandle | None

    def __new__(cls, first: float) -> Self:
        self = super().__new__(cls)
        self._first = first
        self._paths = {}
        self._timer = None
        return self

    @property
    def first(self) -> float:
        """Loop-clock time of the first still-pending event (max-delay anchor)."""
        return self._first

    @property
    def count(self) -> int:
        """Distinct pending paths — what the bulk threshold is measured against."""
        return len(self._paths)

    def record(self, path: Path, *, deleted: bool) -> None:
        """Last-event-wins: only the final state of *path* matters."""
        self._paths[path] = deleted

    def arm(self, timer: asyncio.TimerHandle) -> None:
        """Replace the pending timer, cancelling any prior one."""
        self.cancel_timer()
        self._timer = timer

    def cancel_timer(self) -> None:
        """Cancel the pending timer if one is armed."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def modified(self) -> tuple[Path, ...]:
        """The paths whose last event was a create/modify."""
        return tuple(p for p, deleted in self._paths.items() if not deleted)

    def deleted(self) -> tuple[Path, ...]:
        """The paths whose last event was a deletion."""
        return tuple(p for p, deleted in self._paths.items() if deleted)


@final
class DebouncedDispatcher:
    """Coalesce per-path fs events into one :class:`FlushBatch` per quiet window."""

    __slots__ = (
        "_bulk_threshold",
        "_debounce",
        "_loop",
        "_max_delay",
        "_pending",
        "_sink",
    )

    _loop: asyncio.AbstractEventLoop
    _debounce: float
    _max_delay: float
    _bulk_threshold: int
    _sink: Callable[[FlushBatch], None]
    _pending: dict[RouteKey, _PendingDir]

    def __new__(
        cls,
        loop: asyncio.AbstractEventLoop,
        *,
        debounce_s: float,
        max_delay_s: float,
        bulk_threshold: int,
        sink: Callable[[FlushBatch], None],
    ) -> Self:
        self = super().__new__(cls)
        self._loop = loop
        self._debounce = debounce_s
        self._max_delay = max_delay_s
        self._bulk_threshold = bulk_threshold
        self._sink = sink
        self._pending = {}
        return self

    def feed(self, key: RouteKey, event: FsEvent) -> None:
        """Record *event* under *key* and (re)arm that key's debounce timer.

        Called on the event loop (the watch loop marshals observer-thread events
        here).  Never raises: an arming failure is logged, not propagated, so a
        stray event can never crash the loop (bug-class 2).
        """
        try:
            pending = self._pending.get(key)
            if pending is None:
                pending = _PendingDir(self._loop.time())
                self._pending[key] = pending
            pending.record(event.path, deleted=event.deleted)
            self._arm(key, pending)
        except Exception:
            logger.exception("watch: debounce feed failed for %s", key)

    def cancel(self, key: RouteKey) -> None:
        """Drop all pending changes for *key* (deregister/stop-watching)."""
        pending = self._pending.pop(key, None)
        if pending is not None:
            pending.cancel_timer()

    def cancel_all(self) -> None:
        """Cancel every pending timer (loop shutdown)."""
        for pending in self._pending.values():
            pending.cancel_timer()
        self._pending.clear()

    def pending_count(self) -> int:
        """Return how many route keys currently have pending changes (tests)."""
        return len(self._pending)

    def _arm(self, key: RouteKey, pending: _PendingDir) -> None:
        """Schedule the flush at ``debounce`` from now, capped by ``max_delay``."""
        now = self._loop.time()
        deadline = min(now + self._debounce, pending.first + self._max_delay)
        delay = max(0.0, deadline - now)
        pending.arm(self._loop.call_later(delay, self._flush, key))

    def _flush(self, key: RouteKey) -> None:
        """Emit the coalesced batch for *key*; never let the sink crash the loop."""
        pending = self._pending.pop(key, None)
        if pending is None:
            return
        pending.cancel_timer()
        batch = self._batch(key, pending)
        try:
            self._sink(batch)
        except Exception:
            logger.exception("watch: batch sink failed for %s", key)

    def _batch(self, key: RouteKey, pending: _PendingDir) -> FlushBatch:
        """Build the per-file or bulk batch from *pending*'s accumulated paths."""
        if pending.count > self._bulk_threshold:
            return FlushBatch(key, (), (), bulk=True)
        return FlushBatch(key, pending.modified(), pending.deleted(), bulk=False)
