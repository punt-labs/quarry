"""LoopClose: wait for what an aborted queue left running, then report what is left."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast, final

from quarry.daemon.abort_tally import AbortTally
from quarry.daemon.job_spool import SpoolRecord
from quarry.daemon.loop_close import LoopClose
from quarry.daemon.settlement import Settlement
from quarry.daemon.store_run import StoreRun
from tests.job_states import JobStates

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from quarry.daemon.collection_worker import CollectionWorker
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.tasks import TaskState


@final
@dataclass(frozen=True, slots=True)
class _Job:
    """The job a store belongs to: only its collection is read."""

    collection: str = "c9"

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        del ctx, state

    def spool_record(self) -> SpoolRecord | None:
        return None


@final
class _Gate:
    """A store call that waits for the test to let it end."""

    __slots__ = ("_release",)

    _release: threading.Event

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._release = threading.Event()
        return self

    def __call__(self) -> dict[str, object]:
        self._release.wait(5)
        return {"chunks": 1}

    def open(self) -> None:
        self._release.set()


def _store(gate: Callable[[], dict[str, object]], task: str) -> StoreRun:
    state = JobStates.reaching("running")
    state.task_id = task
    return StoreRun.start(_Job(), state, gate, Settlement.done)


@final
class _Worker:
    """A worker with stores to wait for, stragglers to name and removals to drain."""

    __slots__ = ("_removals", "_stores", "_stragglers", "_tally")

    _removals: tuple[Path, ...]
    _stragglers: tuple[StoreRun, ...]
    _stores: tuple[StoreRun, ...]
    _tally: AbortTally

    def __new__(
        cls,
        stores: tuple[StoreRun, ...] = (),
        removals: tuple[Path, ...] = (),
        stragglers: tuple[StoreRun, ...] = (),
    ) -> Self:
        self = super().__new__(cls)
        self._removals = removals
        self._stragglers = stragglers
        self._stores = stores
        self._tally = AbortTally()
        return self

    @property
    def tally(self) -> AbortTally:
        return self._tally

    def unobserved_stores(self) -> tuple[StoreRun, ...]:
        return tuple(s for s in self._stores if s.is_unobserved)

    def straggling_stores(self) -> tuple[StoreRun, ...]:
        return self._stragglers

    async def drain_removals(self, timeout: float) -> tuple[Path, ...]:
        del timeout
        return self._removals


def _close(*workers: _Worker) -> LoopClose:
    return LoopClose(cast("list[CollectionWorker]", list(workers)))


def _named(caplog: pytest.LogCaptureFixture, text: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if text in r.getMessage()]


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
    gate = _Gate()
    store = _store(gate, "inside")
    store.detach()
    asyncio.get_running_loop().call_later(0.05, gate.open)

    with caplog.at_level(logging.WARNING):
        await _close(_Worker((store,))).run(await _deadline(2.0))

    assert _named(caplog, "still running") == []
    assert not store.is_unobserved


async def test_a_store_outlasting_the_deadline_is_named_once_by_kind_collection_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = _Gate()
    store = _store(gate, "stuck")
    store.detach()

    loop = asyncio.get_running_loop()
    started = loop.time()
    with caplog.at_level(logging.WARNING):
        await _close(_Worker((store,))).run(await _deadline(0.1))
    gate.open()

    assert loop.time() - started < 1.0
    assert _named(caplog, "still running") == [
        (
            "remember _Job for c9 (task stuck): store still running at shutdown; "
            "its spool record (if any) may duplicate stored content"
        )
    ]


async def test_a_straggling_workers_in_flight_store_is_named_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = _Gate()
    store = _store(gate, "straggler")

    with caplog.at_level(logging.WARNING):
        await _close(_Worker(stragglers=(store,))).run(await _deadline(0.05))
    gate.open()

    assert _named(caplog, "(task straggler): store still running") != []


async def test_a_removal_still_running_is_named_by_file(
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = _Worker(removals=(Path("/spool/remember-1.json"),))

    with caplog.at_level(logging.WARNING):
        await _close(worker).run(await _deadline(0.1))

    [named] = _named(caplog, "remember-1.json")
    assert "was still running at shutdown; it may stay on disk" in named
    assert "/spool" not in named


async def test_the_wait_never_cancels_the_store_it_waits_for() -> None:
    gate = _Gate()
    store = _store(gate, "kept")
    store.detach()

    await _close(_Worker((store,))).run(await _deadline(0.05))

    assert store.is_unobserved  # still waiting on its thread, not cancelled
    gate.open()
    await asyncio.wait_for(store.observed(), 5)
    assert store.outcome() == Settlement.done({"chunks": 1})
