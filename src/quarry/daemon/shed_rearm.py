"""Re-arm filesystem events the ingest queue shed, with exponential backoff.

A full queue (503) is transient and the file on disk is durable, so a shed
change is delayed and never dropped: the events go back through the debouncer
after a delay that doubles per consecutive failure for the same routing key,
capped so a persistently full queue still retries at a steady rate.

Separated from :class:`~quarry.daemon.watch_submit.WatchSubmitter` because the
two share no state.  Submitting reads the roster, the context, and the finalize
throttle; re-arming owns the per-key delay, the pending timer handles, and the
debouncer to feed them back into — a disjoint set, which is the definition of a
second responsibility living in one class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Sequence

    from quarry.daemon.debounce import DebouncedDispatcher
    from quarry.daemon.fs_events import FsEvent
    from quarry.daemon.route_key import RouteKey

# A shed live submit re-arms after this delay, doubling to the cap.
_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 30.0


@final
class ShedEventRearmer:
    """Hold shed events and feed them back through the debouncer, backing off."""

    __slots__ = ("_backoff", "_dispatcher", "_loop", "_timers")

    _loop: asyncio.AbstractEventLoop
    # None until bind(): the dispatcher's sink is the submitter, so the
    # dispatcher cannot exist before the thing that re-arms through it.
    _dispatcher: DebouncedDispatcher | None
    _backoff: dict[RouteKey, float]
    # Pending re-arm timers, tracked so shutdown can cancel them.
    _timers: set[asyncio.TimerHandle]

    def __new__(cls, loop: asyncio.AbstractEventLoop) -> Self:
        """Prepare retry timers without attaching a debouncer or arming events."""
        self = super().__new__(cls)
        self._loop = loop
        self._dispatcher = None
        self._backoff = {}
        self._timers = set()
        return self

    def bind(self, dispatcher: DebouncedDispatcher) -> None:
        """Wire the debouncer shed events are fed back through."""
        self._dispatcher = dispatcher

    def reset(self, key: RouteKey) -> None:
        """Drop *key*'s accumulated delay, so its next shed starts from the base.

        Both callers want exactly this: a batch that cleared without shedding
        has no failure left to back off from, and a deregistered key has no
        events left at all.
        """
        self._backoff.pop(key, None)

    def cancel_pending(self) -> None:
        """Cancel every outstanding re-arm so none fires after shutdown."""
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()

    def defer(self, key: RouteKey, events: list[FsEvent]) -> None:
        """Schedule *events* to be re-fed after *key*'s current backoff delay."""
        delay = self._backoff.get(key, _BACKOFF_BASE_S)
        self._backoff[key] = min(delay * 2, _BACKOFF_MAX_S)
        # Prune fired handles so the set stays bounded, then track the new one so
        # shutdown can cancel a still-pending re-arm (no stray timer post-stop).
        now = self._loop.time()
        self._timers = {timer for timer in self._timers if timer.when() > now}
        self._timers.add(self._loop.call_later(delay, self._refeed, key, tuple(events)))

    def _refeed(self, key: RouteKey, events: Sequence[FsEvent]) -> None:
        """Feed deferred events back through the debouncer for another attempt."""
        if self._dispatcher is None:
            return
        for event in events:
            self._dispatcher.feed(key, event)
