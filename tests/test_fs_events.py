"""Hermetic tests for the debounce dispatcher and the fs-event seam (DES-045).

The dispatcher is driven directly on a real event loop with tiny debounce/max
windows and a recording sink, so coalescing, the bulk threshold, anti-starvation,
and the never-crash-the-loop contract are asserted without watchdog or the
filesystem.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.debounce import DebouncedDispatcher
from quarry.daemon.fs_events import FsEvent
from quarry.daemon.route_key import RouteKey

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.debounce import FlushBatch

_KEY = RouteKey("db", "col")


@final
class _Sink:
    """Record every batch the dispatcher emits; optionally raise once."""

    __slots__ = ("_boom", "batches")

    batches: list[FlushBatch]
    _boom: bool

    def __new__(cls, *, boom: bool = False) -> Self:
        self = super().__new__(cls)
        self.batches = []
        self._boom = boom
        return self

    def __call__(self, batch: FlushBatch) -> None:
        self.batches.append(batch)
        if self._boom:
            self._boom = False
            msg = "sink boom"
            raise RuntimeError(msg)


def _dispatcher(
    sink: Callable[[FlushBatch], None],
    *,
    debounce: float = 0.03,
    max_delay: float = 0.3,
    bulk: int = 5,
) -> DebouncedDispatcher:
    return DebouncedDispatcher(
        asyncio.get_running_loop(),
        debounce_s=debounce,
        max_delay_s=max_delay,
        bulk_threshold=bulk,
        sink=sink,
    )


def test_debounce_coalesces_ten_events_to_one_job() -> None:
    """Ten modifies of one path inside the window emit exactly one batch."""

    async def _run() -> None:
        sink = _Sink()
        dispatcher = _dispatcher(sink)
        for _ in range(10):
            dispatcher.feed(_KEY, FsEvent(Path("/r/a.md"), deleted=False))
        await asyncio.sleep(0.1)
        assert len(sink.batches) == 1
        assert sink.batches[0].modified == (Path("/r/a.md"),)
        assert sink.batches[0].bulk is False

    asyncio.run(_run())


def test_max_delay_flushes_a_continuously_rearmed_path() -> None:
    """A path re-armed past max_delay is flushed anyway (no starvation)."""

    async def _run() -> None:
        sink = _Sink()
        dispatcher = _dispatcher(sink, debounce=0.05, max_delay=0.12)
        # Re-arm every 0.02 s for ~0.24 s: the debounce never elapses on its own,
        # so only the max-delay cap can force the flush.
        for _ in range(12):
            dispatcher.feed(_KEY, FsEvent(Path("/r/a.md"), deleted=False))
            await asyncio.sleep(0.02)
        assert len(sink.batches) >= 1

    asyncio.run(_run())


def test_bulk_threshold_collapses_to_one_bulk_batch() -> None:
    """More than bulk_threshold distinct paths emit one bulk batch, no per-file."""

    async def _run() -> None:
        sink = _Sink()
        dispatcher = _dispatcher(sink, bulk=5)
        for i in range(6):
            dispatcher.feed(_KEY, FsEvent(Path(f"/r/f{i}.md"), deleted=False))
        await asyncio.sleep(0.1)
        assert len(sink.batches) == 1
        assert sink.batches[0].bulk is True
        assert sink.batches[0].modified == ()
        assert sink.batches[0].deleted == ()

    asyncio.run(_run())


def test_modified_and_deleted_split_in_one_batch() -> None:
    """A modify and a delete in the same window land in the right tuples."""

    async def _run() -> None:
        sink = _Sink()
        dispatcher = _dispatcher(sink)
        dispatcher.feed(_KEY, FsEvent(Path("/r/a.md"), deleted=False))
        dispatcher.feed(_KEY, FsEvent(Path("/r/b.md"), deleted=True))
        await asyncio.sleep(0.1)
        assert sink.batches[0].modified == (Path("/r/a.md"),)
        assert sink.batches[0].deleted == (Path("/r/b.md"),)

    asyncio.run(_run())


def test_last_event_wins_delete_then_recreate() -> None:
    """A delete followed by a modify of one path resolves to modify (last wins)."""

    async def _run() -> None:
        sink = _Sink()
        dispatcher = _dispatcher(sink)
        dispatcher.feed(_KEY, FsEvent(Path("/r/a.md"), deleted=True))
        dispatcher.feed(_KEY, FsEvent(Path("/r/a.md"), deleted=False))
        await asyncio.sleep(0.1)
        assert sink.batches[0].modified == (Path("/r/a.md"),)
        assert sink.batches[0].deleted == ()

    asyncio.run(_run())


def test_cancel_drops_pending_paths() -> None:
    """cancel(key) discards pending changes so no batch is emitted."""

    async def _run() -> None:
        sink = _Sink()
        dispatcher = _dispatcher(sink)
        dispatcher.feed(_KEY, FsEvent(Path("/r/a.md"), deleted=False))
        dispatcher.cancel(_KEY)
        await asyncio.sleep(0.1)
        assert sink.batches == []

    asyncio.run(_run())


def test_raising_sink_never_crashes_the_loop() -> None:
    """A sink that raises on one batch is caught; later batches still deliver."""

    async def _run() -> None:
        sink = _Sink(boom=True)
        dispatcher = _dispatcher(sink)
        dispatcher.feed(_KEY, FsEvent(Path("/r/a.md"), deleted=False))
        await asyncio.sleep(0.1)  # first flush raises inside the sink, is swallowed
        other = RouteKey("db", "col2")
        dispatcher.feed(other, FsEvent(Path("/r/b.md"), deleted=False))
        await asyncio.sleep(0.1)  # loop survived; the second batch still arrives
        assert len(sink.batches) == 2

    asyncio.run(_run())
