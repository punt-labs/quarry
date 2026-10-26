"""LoopClose: wait for what an aborted queue left running, then report what is left."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast, final

from quarry.daemon.abort_tally import AbortTally
from quarry.daemon.loop_close import LoopClose

if TYPE_CHECKING:
    import pytest

    from quarry.daemon.collection_worker import CollectionWorker


@final
class _Store:
    """A store the loop is waiting for: observed when the test says so."""

    __slots__ = ("_event", "_label")

    _event: asyncio.Event
    _label: str

    def __new__(cls, label: str) -> _Store:
        self = super().__new__(cls)
        self._event = asyncio.Event()
        self._label = label
        return self

    @property
    def is_unobserved(self) -> bool:
        return not self._event.is_set()

    async def observed(self) -> None:
        await self._event.wait()

    def describe(self) -> str:
        return self._label

    def observe(self) -> None:
        self._event.set()


@final
class _Worker:
    """A worker with stores to wait for and removals to drain."""

    __slots__ = ("_removals", "_stores", "tally")

    _removals: tuple[Path, ...]
    _stores: tuple[_Store, ...]
    tally: AbortTally

    def __new__(
        cls, stores: tuple[_Store, ...] = (), removals: tuple[Path, ...] = ()
    ) -> _Worker:
        self = super().__new__(cls)
        self._removals = removals
        self._stores = stores
        self.tally = AbortTally()
        return self

    def unobserved_stores(self) -> tuple[_Store, ...]:
        return tuple(s for s in self._stores if s.is_unobserved)

    async def drain_removals(self, timeout: float) -> tuple[Path, ...]:
        del timeout
        return self._removals


def _close(*workers: _Worker) -> LoopClose:
    return LoopClose(cast("list[CollectionWorker]", list(workers)))


async def _deadline(seconds: float) -> float:
    return asyncio.get_running_loop().time() + seconds


async def test_an_empty_queue_reports_only_the_tally(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        await _close(_Worker()).run(await _deadline(1.0))

    [record] = caplog.records
    assert record.getMessage().startswith("ingest queue: after the drain, 0 job(s)")


async def test_a_store_observed_inside_the_budget_is_waited_for_and_not_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _Store("remember X for c (task t)")
    asyncio.get_running_loop().call_later(0.05, store.observe)

    with caplog.at_level(logging.WARNING):
        await _close(_Worker((store,))).run(await _deadline(2.0))

    assert [
        r.getMessage() for r in caplog.records if "still running" in r.getMessage()
    ] == []


async def test_a_store_outlasting_the_deadline_is_named_once_by_kind_collection_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _Store("remember ScrubbedIngestJob for c9 (task stuck)")

    loop = asyncio.get_running_loop()
    started = loop.time()
    with caplog.at_level(logging.WARNING):
        await _close(_Worker((store,))).run(await _deadline(0.1))

    assert loop.time() - started < 1.0
    named = [
        r.getMessage() for r in caplog.records if "still running" in r.getMessage()
    ]
    assert named == [
        (
            "remember ScrubbedIngestJob for c9 (task stuck): store still running "
            "at shutdown; its spool record (if any) may duplicate stored content"
        )
    ]


async def test_a_removal_still_running_is_named_by_file(
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = _Worker(removals=(Path("/spool/remember-1.json"),))

    with caplog.at_level(logging.WARNING):
        await _close(worker).run(await _deadline(0.1))

    [named] = [
        r.getMessage() for r in caplog.records if "remember-1.json" in r.getMessage()
    ]
    assert "was still running at shutdown; it may stay on disk" in named
    assert "/spool" not in named


async def test_the_wait_never_cancels_the_store_it_waits_for() -> None:
    store = _Store("s")

    await _close(_Worker((store,))).run(await _deadline(0.05))

    assert store.is_unobserved
    store.observe()
    assert not store.is_unobserved
