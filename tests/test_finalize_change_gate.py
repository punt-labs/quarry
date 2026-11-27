"""A finalize rewrites the table only when the table changed since its last one.

Every test drives the real submit path (the safety-scan sweep, a watch batch, an
explicit sync, a daemon start) into a queue that RUNS the admitted finalize jobs
in per-key FIFO order against a real LanceDB database.  "Table-wide work" is counted at
the two calls that rewrite the table: ``TableOptimizer.optimize`` (compaction,
cleanup, full-text rebuild) and ``TableOptimizer.create_collection_index``.

The file is long by design: every test shares one fake queue and one fake daemon
harness for one behavior, the change-gated finalize, and splitting it would
duplicate that harness in each part.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections import Counter
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Self, cast, final

import numpy as np
import pytest
from lancedb.index import FTS, Bitmap, BTree

from quarry.config import Settings
from quarry.daemon.collection_finalize_job import CollectionFinalizeJob
from quarry.daemon.finalize_pacing import FinalizeThrottle
from quarry.daemon.finalize_retry import FinalizeRetry
from quarry.daemon.fs_events import FsEvent
from quarry.daemon.index_jobs import CollectionSyncJob, FileIndexJob
from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import TaskRegistry
from quarry.daemon.watch_loop import WatchLoop
from quarry.daemon.watch_roster import WatchRoster
from quarry.db import Database, TableOptimizer
from quarry.db.lance_connection import LanceConnection
from quarry.db.optimizer import OptimizeOutcome
from quarry.db.orphan_pages import OrphanPageSweep
from quarry.db.recycling_table import RecyclingTable
from quarry.db.table_changes import TableChanges
from quarry.finalize_ledger import FinalizeLedgers
from quarry.finalize_steps import ShadowPushStep
from quarry.ingestion.pipeline import plan_file_chunks
from quarry.optimize_step import ManualOptimize, OptimizeReport
from quarry.post_sync_gc import PostSyncGc
from quarry.sync_registry import SyncRegistry
from quarry.table_upkeep import TableUpkeep
from tests.manual_clock import ManualLoop

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.task_state import TaskState

_DATABASE = "testdb"


@final
class _Rewrites:
    """Count the calls that rewrite the table, and run a hook inside the rewrite."""

    __slots__ = ("during", "failures", "optimizes", "outcomes", "scalar_indexes")

    optimizes: int
    scalar_indexes: int
    failures: list[Exception]
    outcomes: list[OptimizeOutcome]
    during: Callable[[], None] | None

    def __new__(cls, monkeypatch: pytest.MonkeyPatch) -> Self:
        self = super().__new__(cls)
        self.optimizes = 0
        self.scalar_indexes = 0
        self.failures = []
        self.outcomes = []
        self.during = None
        real_optimize = TableOptimizer.optimize
        real_index = TableOptimizer.create_collection_index

        def optimize(opt: TableOptimizer, *, force: bool = False) -> OptimizeOutcome:
            self.optimizes += 1
            if self.during is not None:
                self.during()
            if self.failures:
                raise self.failures.pop(0)
            outcome = real_optimize(opt, force=force)
            self.outcomes.append(outcome)
            return outcome

        def index(opt: TableOptimizer) -> None:
            self.scalar_indexes += 1
            real_index(opt)

        monkeypatch.setattr(TableOptimizer, "optimize", optimize)
        monkeypatch.setattr(TableOptimizer, "create_collection_index", index)
        return self


@final
class _ExecutingQueue:
    """Admit every job and run it: one FIFO per route key, as in production.

    Each ``(database, collection)`` key gets its own worker task, so a job on one
    key can overtake a queued job on another, which is what the real ingest queue
    allows.  A global semaphore of one stands in for the embed gate that serializes
    every job's run body.  Scans run via a hook, finalizes for real.
    """

    __slots__ = (
        "_ctx",
        "_gates",
        "_pending",
        "_serial",
        "_workers",
        "file_jobs",
        "finalizes",
        "on_index",
        "on_scan",
        "shed",
    )

    _ctx: DaemonContext | None
    _pending: dict[RouteKey, asyncio.Queue[tuple[IngestUnit, TaskState]]]
    _workers: list[asyncio.Task[None]]
    _gates: dict[str | None, asyncio.Event]
    _serial: asyncio.Semaphore
    on_scan: Callable[[], None]
    on_index: Callable[[], None]
    finalizes: int
    file_jobs: Counter[str]
    shed: bool

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.finalizes = 0
        self.file_jobs = Counter()
        self._ctx = None
        self._pending = {}
        self._workers = []
        self._gates = {}
        self._serial = asyncio.Semaphore(1)
        self.on_scan = lambda: None
        self.on_index = lambda: None
        self.shed = False
        return self

    def bind(self, ctx: DaemonContext) -> None:
        self._ctx = ctx

    def hold(self, collection: str | None = None) -> None:
        """Queue jobs without running them until :meth:`release`; ``None`` is all."""
        self._gates[collection] = asyncio.Event()

    def release(self, collection: str | None = None) -> None:
        self._gates[collection].set()

    def try_submit(self, key: RouteKey, job: IngestUnit, state: TaskState) -> bool:
        if self.shed:
            state.fail("ingest queue full")
            return False
        if isinstance(job, FileIndexJob):
            self.file_jobs[key.collection] += 1
        if key not in self._pending:
            self._pending[key] = asyncio.Queue()
            self._workers.append(
                asyncio.get_running_loop().create_task(self._drain(key))
            )
        self._pending[key].put_nowait((job, state))
        return True

    def has_file_job(self, collection: str) -> bool:
        """Return whether a file-index job was admitted for *collection*."""
        return self.file_jobs[collection] >= 1

    async def settled(self) -> None:
        """Wait until every admitted job has run."""
        for pending in list(self._pending.values()):
            await pending.join()

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()

    async def _drain(self, key: RouteKey) -> None:
        pending = self._pending[key]
        while True:
            job, state = await pending.get()
            for gate in (self._gates.get(None), self._gates.get(key.collection)):
                if gate is not None:
                    await gate.wait()
            try:
                async with self._serial:
                    await self._run(job, state)
            finally:
                pending.task_done()

    async def _run(self, job: IngestUnit, state: TaskState) -> None:
        assert self._ctx is not None
        if isinstance(job, CollectionFinalizeJob):
            self.finalizes += 1
            await job.run(self._ctx, state)
            return
        if isinstance(job, CollectionSyncJob):
            self.on_scan()
        elif isinstance(job, FileIndexJob):
            self.on_index()
        state.complete({})


@final
class _FakeSource:
    __slots__ = ("watches",)

    watches: dict[object, tuple[Path, Callable[[FsEvent], None]]]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.watches = {}
        return self

    def schedule(
        self, root: Path, on_event: Callable[[FsEvent], None]
    ) -> object | None:
        handle = object()
        self.watches[handle] = (root, on_event)
        return handle

    def unschedule(self, handle: object | None) -> None:
        self.watches.pop(handle, None)

    def is_alive(self, handle: object | None) -> bool:
        return handle in self.watches

    def stop(self) -> None:
        self.watches.clear()

    def emit(self, root: Path, event: FsEvent) -> None:
        for watched, on_event in list(self.watches.values()):
            if watched == root:
                on_event(event)


@final
class _Daemon:
    """A started WatchLoop over real databases and an executing queue."""

    __slots__ = (
        "clock",
        "collections",
        "ctx",
        "loop",
        "queue",
        "rewrites",
        "roots",
        "source",
    )

    clock: ManualLoop | None
    ctx: DaemonContext
    loop: WatchLoop
    queue: _ExecutingQueue
    source: _FakeSource
    rewrites: _Rewrites
    roots: dict[str, Path]
    collections: tuple[str, ...]

    def __new__(
        cls,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        collections: tuple[str, ...],
        *,
        bulk: int = 50,
        interval: float = 0.0,
        siblings: tuple[str, ...] = (),
        manual_clock: bool = False,
    ) -> Self:
        self = super().__new__(cls)
        self.clock = ManualLoop() if manual_clock else None
        if self.clock is not None:
            # The throttle's interval and trailing timers and the finalize retry
            # run on this clock, so when a timer fires is the test's decision,
            # not the scheduler's.
            clock = self.clock.as_loop
            monkeypatch.setattr(
                "quarry.daemon.watch_submit.FinalizeThrottle",
                lambda _loop, interval_s: FinalizeThrottle(clock, interval_s),
            )
            monkeypatch.setattr(
                "quarry.daemon.watch_submit.FinalizeRetry",
                lambda _loop: FinalizeRetry(clock),
            )
        data = tmp_path / "data"
        (data / _DATABASE).mkdir(parents=True)
        settings = Settings(
            quarry_root=data,
            lancedb_path=data / _DATABASE / "lancedb",
            registry_path=data / _DATABASE / "registry.db",
            watch_enabled=True,
            watch_debounce_s=0.03,
            watch_max_delay_s=0.3,
            watch_bulk_threshold=bulk,
            watch_safety_scan_s=0.0,
            watch_optimize_min_interval_s=interval,
        )
        self.roots = {}
        registry = SyncRegistry(settings.registry_path)
        try:
            for name in collections:
                root = tmp_path / name
                root.mkdir()
                self.roots[name] = root.resolve()
                registry.register_directory(root.resolve(), name)
        finally:
            registry.close()
        for sibling in siblings:
            (data / sibling).mkdir()
            other = SyncRegistry(data / sibling / "registry.db")
            try:
                root = tmp_path / f"root-{sibling}"
                root.mkdir()
                other.register_directory(root.resolve(), "o")
            finally:
                other.close()
        self.queue = _ExecutingQueue()
        self.ctx = cast(
            "DaemonContext",
            SimpleNamespace(
                settings=settings,
                ingest_queue=self.queue,
                tasks=TaskRegistry(),
                database=Database.connect(settings.lancedb_path),
                database_name=_DATABASE,
            ),
        )
        self.queue.bind(self.ctx)
        self.source = _FakeSource()
        self.loop = WatchLoop(self.ctx, source=self.source)
        self.rewrites = _Rewrites(monkeypatch)
        self.collections = collections
        return self

    @property
    def database(self) -> Database:
        roster = self.loop._roster
        assert roster is not None
        return roster.database_of(_DATABASE)

    @property
    def settings(self) -> Settings:
        return self.ctx.settings

    async def start(self) -> None:
        await self.loop.start()
        await self.queue.settled()

    async def stop(self) -> None:
        await self.queue.settled()
        await self.loop.stop()  # cancels the throttle and retry timers
        await self.queue.settled()  # a timer that fired in between admitted a job
        await self.queue.stop()

    async def safety_scan(self) -> None:
        reconciler = self.loop._reconciler
        assert reconciler is not None
        await reconciler.run_once()
        await self.queue.settled()

    async def explicit_sync(self) -> None:
        umbrella = self.ctx.tasks.begin("sync")
        await self.loop.request_scan(umbrella)
        await self.queue.settled()
        assert umbrella.status == "completed"

    def prefill(self) -> None:
        """Put a chunk in the table before the daemon starts."""
        self.write_chunk(database=self.ctx.database)

    def write_chunk(
        self,
        collection: str = "",
        name: str = "doc.md",
        database: Database | None = None,
    ) -> None:
        """Index one document into *collection* through the real chunk store."""
        collection = collection or self.collections[0]
        path = self.roots[collection] / f".{name}"
        path.write_text("indexable body " * 20)
        chunks, _ = plan_file_chunks(
            path, self.settings, collection=collection, document_name=name
        )
        (database or self.database).store.insert(
            chunks, np.zeros((len(chunks), 768), dtype=np.float32)
        )


@pytest.fixture(autouse=True)
def _isolated_ledgers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ledgers are keyed by database name, so every test starts with none."""
    monkeypatch.setattr(FinalizeLedgers, "_ledgers", {})


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Daemon]:
    def make(
        *collections: str,
        bulk: int = 50,
        interval: float = 0.0,
        siblings: tuple[str, ...] = (),
        manual_clock: bool = False,
    ) -> _Daemon:
        return _Daemon(
            tmp_path,
            monkeypatch,
            collections or ("col",),
            bulk=bulk,
            interval=interval,
            siblings=siblings,
            manual_clock=manual_clock,
        )

    return make


def test_start_runs_one_finalize_per_database_however_many_registrations(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("a", "b", "c")
        await d.start()
        assert d.rewrites.optimizes == 1
        await d.stop()

    asyncio.run(run())


def test_unchanged_safety_scan_rewrites_nothing(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("a", "b")
        await d.start()
        before = (d.rewrites.optimizes, d.rewrites.scalar_indexes)
        await d.safety_scan()
        await d.safety_scan()
        assert (d.rewrites.optimizes, d.rewrites.scalar_indexes) == before
        await d.stop()

    asyncio.run(run())


def test_batch_that_admits_no_job_rewrites_nothing(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("col")
        await d.start()
        before = d.rewrites.optimizes
        root = d.roots["col"]
        ignored = root / "notes.unsupported"
        ignored.write_text("not indexable")
        d.source.emit(root, FsEvent(ignored, deleted=False))
        await asyncio.sleep(0.15)
        await d.queue.settled()
        assert d.rewrites.optimizes == before
        await d.stop()

    asyncio.run(run())


def test_bulk_batch_over_unchanged_files_rewrites_nothing(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("col", bulk=2)
        await d.start()
        before = d.rewrites.optimizes
        root = d.roots["col"]
        for i in range(3):
            path = root / f"f{i}.md"
            path.write_text("body")
            d.source.emit(root, FsEvent(path, deleted=False))
        await asyncio.sleep(0.15)
        await d.queue.settled()
        assert d.rewrites.optimizes == before
        await d.stop()

    asyncio.run(run())


def test_explicit_sync_of_unchanged_collection_rewrites_nothing(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("col")
        await d.start()
        before = d.rewrites.optimizes
        await d.explicit_sync()
        await d.explicit_sync()
        assert d.rewrites.optimizes == before
        await d.stop()

    asyncio.run(run())


def test_scan_ahead_of_the_finalize_that_writes_makes_it_rewrite(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("col")
        await d.start()
        before = d.rewrites.optimizes
        d.queue.on_scan = d.write_chunk
        await d.safety_scan()
        assert d.rewrites.optimizes == before + 1
        await d.stop()

    asyncio.run(run())


def test_write_in_another_collection_of_the_same_database_makes_it_rewrite(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("a", "b")
        await d.start()
        before = d.rewrites.optimizes
        d.write_chunk("b")
        await d.safety_scan()
        assert d.rewrites.optimizes == before + 1
        await d.stop()

    asyncio.run(run())


def test_failed_finalize_leaves_the_database_dirty(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("col", interval=30.0)
        d.rewrites.failures = [RuntimeError("compaction failed")]
        await d.start()
        assert d.rewrites.optimizes == 1
        FinalizeLedgers.for_database(
            d.settings.lancedb_path, d.settings.quarry_root
        ).clear()
        await d.safety_scan()
        assert d.rewrites.optimizes == 2
        await d.safety_scan()
        assert d.rewrites.optimizes == 2
        await d.stop()

    asyncio.run(run())


def test_write_during_a_finalize_leaves_the_database_dirty(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("col", interval=30.0)
        await d.start()
        d.write_chunk()
        landed = threading.Event()

        def write_mid_rewrite() -> None:
            if not landed.is_set():
                landed.set()
                d.write_chunk(name="late.md")

        d.rewrites.during = write_mid_rewrite
        await d.safety_scan()
        assert landed.is_set()
        rewrites = d.rewrites.optimizes
        d.rewrites.during = None
        await d.safety_scan()
        assert d.rewrites.optimizes == rewrites + 1
        await d.safety_scan()
        assert d.rewrites.optimizes == rewrites + 1
        await d.stop()

    asyncio.run(run())


def test_skipped_finalize_logs_database_and_reason(
    daemon: Callable[..., _Daemon], caplog: pytest.LogCaptureFixture
) -> None:
    async def run() -> None:
        d = daemon("col")
        await d.start()
        with caplog.at_level("DEBUG", logger="quarry.table_upkeep"):
            await d.safety_scan()
        await d.stop()

    asyncio.run(run())
    skipped = [
        r.getMessage() for r in caplog.records if "finalize skipped" in r.message
    ]
    assert len(skipped) == 1
    assert _DATABASE in skipped[0]
    assert "unchanged" in skipped[0]


def _throttle(d: _Daemon) -> FinalizeThrottle:
    submitter = d.loop._submitter
    assert submitter is not None
    return submitter._throttle


def _armed_trailing(d: _Daemon) -> bool:
    return bool(_throttle(d)._timers)


def _arm_trailing_finalize(d: _Daemon) -> None:
    """A watched edit inside the throttle interval arms a trailing finalize."""
    path = d.roots["col"] / "edited.md"
    path.write_text("body")
    d.source.emit(d.roots["col"], FsEvent(path, deleted=False))


def test_skipped_finalize_leaves_an_armed_trailing_finalize_alone(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("col", interval=30.0)
        await d.start()
        _arm_trailing_finalize(d)
        await asyncio.sleep(0.15)
        assert _armed_trailing(d)
        await d.safety_scan()
        assert _armed_trailing(d)
        await d.stop()

    asyncio.run(run())


async def _until(predicate: Callable[[], bool], seconds: float = 15.0) -> None:
    """Poll until *predicate* holds, then let the caller assert on the result."""
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)


async def _sweep_finalize_ahead_of_an_edit(d: _Daemon) -> None:
    """Hold the queue, submit a scan and sweep finalize, then arm a trailing one."""
    d.queue.hold()
    reconciler = d.loop._reconciler
    assert reconciler is not None
    await reconciler.run_once()
    _arm_trailing_finalize(d)
    await _until(lambda: _armed_trailing(d))
    assert _armed_trailing(d)
    d.queue.release()
    await d.queue.settled()


def test_trailing_finalize_fires_and_skips_when_nothing_landed_after_the_rewrite(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("col", interval=4.0, manual_clock=True)
        assert d.clock is not None
        await d.start()
        base = d.rewrites.optimizes
        finalizes = d.queue.finalizes
        d.queue.on_scan = d.write_chunk
        await _sweep_finalize_ahead_of_an_edit(d)
        assert d.rewrites.optimizes == base + 1
        d.clock.advance(4.0)  # the trailing timer comes due
        await _until(lambda: d.queue.finalizes >= finalizes + 2)
        await d.queue.settled()
        assert d.queue.finalizes == finalizes + 2  # the sweep's and the trailing one
        assert d.rewrites.optimizes == base + 1  # the trailing one skipped
        await d.stop()

    asyncio.run(run())


def test_trailing_finalize_rewrites_a_write_that_landed_after_the_sweep_rewrite(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("col", interval=4.0, manual_clock=True)
        assert d.clock is not None
        await d.start()
        base = d.rewrites.optimizes
        landed = [False]

        def late_write() -> None:
            if not landed[0]:
                landed[0] = True
                d.write_chunk(name="late.md")

        d.queue.on_scan = d.write_chunk
        d.queue.on_index = late_write
        await _sweep_finalize_ahead_of_an_edit(d)
        assert landed[0]
        d.clock.advance(4.0)  # the trailing timer comes due
        await _until(lambda: d.rewrites.optimizes >= base + 2)
        # The counter moves when optimize STARTS; wait for the finalize to end, or
        # its store thread outlives the test and sweeps under the next test's stub.
        await d.queue.settled()
        assert d.rewrites.optimizes == base + 2  # the sweep's, then the trailing one
        await d.stop()

    asyncio.run(run())


def test_quiet_database_still_sweeps_orphan_pages_on_the_slow_clock(
    daemon: Callable[..., _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    sweeps: list[int] = []

    def counting_sweep(sweep: OrphanPageSweep, now: datetime | None = None) -> int:
        del sweep, now
        sweeps.append(1)
        return 0

    monkeypatch.setattr(OrphanPageSweep, "run", counting_sweep)

    async def run() -> None:
        d = daemon("col")
        await d.start()
        assert len(sweeps) == 1  # the start finalize rewrote the table and swept
        rewrites = d.rewrites.optimizes
        await d.safety_scan()
        assert len(sweeps) == 1  # quiet, and the slow clock is not due
        monkeypatch.setattr(TableChanges, "MAINTENANCE_INTERVAL_S", 0.0)
        await d.safety_scan()
        assert len(sweeps) == 2  # quiet, and the slow clock is due
        assert d.rewrites.optimizes == rewrites
        await d.stop()

    asyncio.run(run())


def test_shadow_push_runs_on_a_skipped_finalize(
    daemon: Callable[..., _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    pushes: list[int] = []
    monkeypatch.setattr(ShadowPushStep, "run", lambda step: pushes.append(1))

    async def run() -> None:
        d = daemon("col")
        await d.start()
        rewrites = d.rewrites.optimizes
        before = len(pushes)
        await d.safety_scan()
        assert len(pushes) == before + 1
        assert d.rewrites.optimizes == rewrites
        await d.stop()

    asyncio.run(run())


def _fail_fts_while(monkeypatch: pytest.MonkeyPatch, failing: list[bool]) -> None:
    """Make only the full-text rebuild raise while ``failing[0]`` is true."""
    real = RecyclingTable.create_index

    def create_index(
        table: RecyclingTable,
        column: str,
        *,
        config: FTS | Bitmap | BTree,
        replace: bool = False,
    ) -> None:
        if failing[0] and isinstance(config, FTS):
            msg = "full-text rebuild failed"
            raise RuntimeError(msg)
        real(table, column, config=config, replace=replace)

    monkeypatch.setattr(RecyclingTable, "create_index", create_index)


def test_failed_fts_rebuild_leaves_the_table_dirty_and_the_next_scan_rewrites(
    daemon: Callable[..., _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = [True]
    _fail_fts_while(monkeypatch, failing)
    monkeypatch.setattr("quarry.finalize_ledger.BACKOFF_BASE_SECONDS", 0.0)

    async def run() -> None:
        d = daemon("col", interval=30.0)
        d.prefill()
        await d.start()
        before = d.rewrites.optimizes
        failing[0] = False
        await d.safety_scan()
        assert d.rewrites.optimizes == before + 1
        await d.safety_scan()
        assert d.rewrites.optimizes == before + 1
        await d.stop()

    asyncio.run(run())


def test_failed_fts_rebuild_is_recorded_in_the_ledger(
    daemon: Callable[..., _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_fts_while(monkeypatch, [True])

    async def run() -> None:
        d = daemon("col", interval=30.0)
        d.prefill()
        await d.start()
        ledger = FinalizeLedgers.for_database(
            d.settings.lancedb_path, d.settings.quarry_root
        )
        assert ledger.health().consecutive_failures == 1
        assert d.rewrites.outcomes[-1].fts_rebuilt is False
        await d.stop()

    asyncio.run(run())


def test_backing_off_finalize_is_a_skip_until_the_backoff_elapses(
    daemon: Callable[..., _Daemon],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sweeps: list[int] = []
    collections: list[int] = []

    def counting_sweep(sweep: OrphanPageSweep, now: datetime | None = None) -> int:
        del sweep, now
        sweeps.append(1)
        return 0

    def counting_gc(gc: PostSyncGc) -> None:
        del gc
        collections.append(1)

    monkeypatch.setattr(OrphanPageSweep, "run", counting_sweep)
    monkeypatch.setattr(PostSyncGc, "run", counting_gc)

    async def run() -> None:
        now = [1000.0]
        d = daemon("col", interval=30.0)
        ledger = FinalizeLedgers.for_database(
            d.settings.lancedb_path, d.settings.quarry_root
        )
        ledger._clock = lambda: now[0]
        d.rewrites.failures = [RuntimeError("boom")]
        await d.start()
        assert d.rewrites.optimizes == 1
        swept, collected = len(sweeps), len(collections)
        with caplog.at_level("DEBUG", logger="quarry.table_upkeep"):
            await d.safety_scan()
        assert d.rewrites.optimizes == 1
        assert (len(sweeps), len(collections)) == (swept, collected)
        assert any("backing off" in r.getMessage() for r in caplog.records)
        now[0] += 10_000.0
        await d.safety_scan()
        assert d.rewrites.optimizes == 2
        await d.stop()

    asyncio.run(run())


def test_a_failed_finalize_is_retried_with_the_safety_scan_disabled(
    daemon: Callable[..., _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("quarry.finalize_ledger.BACKOFF_BASE_SECONDS", 0.0)

    async def run() -> None:
        d = daemon("col", interval=0.05)
        d.rewrites.failures = [RuntimeError("boom")]
        await d.start()
        await _until(lambda: d.rewrites.optimizes >= 2)
        await d.queue.settled()
        assert d.rewrites.optimizes == 2
        await d.stop()

    asyncio.run(run())


def test_an_unexpected_exception_still_arms_a_retry(
    daemon: Callable[..., _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("quarry.finalize_ledger.BACKOFF_BASE_SECONDS", 0.0)

    async def run() -> None:
        d = daemon("col", interval=0.05)
        d.rewrites.failures = [KeyError("unexpected")]
        await d.start()
        await _until(lambda: d.rewrites.optimizes >= 2)
        await d.queue.settled()
        assert d.rewrites.optimizes == 2
        await d.stop()

    asyncio.run(run())


def test_a_retry_shed_by_a_full_queue_is_armed_again(
    daemon: Callable[..., _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("quarry.finalize_ledger.BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr("quarry.daemon.finalize_retry.SHED_RETRY_S", 0.1)

    async def run() -> None:
        d = daemon("col", interval=0.05)
        d.rewrites.failures = [RuntimeError("boom")]
        await d.start()
        d.queue.shed = True
        await asyncio.sleep(1.5)  # the retry fires into a full queue
        assert d.rewrites.optimizes == 1
        d.queue.shed = False
        await _until(lambda: d.rewrites.optimizes >= 2)
        await d.queue.settled()
        assert d.rewrites.optimizes == 2
        await d.stop()

    asyncio.run(run())


def test_manual_optimize_whose_fts_rebuild_fails_dirties_the_record(
    daemon: Callable[..., _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = [False]
    _fail_fts_while(monkeypatch, failing)
    monkeypatch.setattr("quarry.finalize_ledger.BACKOFF_BASE_SECONDS", 0.0)

    async def run() -> None:
        d = daemon("col", interval=30.0)
        d.prefill()
        await d.start()
        await d.safety_scan()
        before = d.rewrites.optimizes
        failing[0] = True
        outcome = ManualOptimize(d.database, d.settings).run_manually(force=False)
        assert outcome.optimized
        assert outcome.fts_rebuilt is False
        failing[0] = False
        await d.safety_scan()
        assert d.rewrites.optimizes == before + 2  # the manual run, then the finalize
        await d.stop()

    asyncio.run(run())


def test_a_finalizer_reads_the_record_of_its_own_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job's db is at path A while its settings say path B."""
    rewrites = _Rewrites(monkeypatch)
    root = tmp_path / "data"
    a, b = root / "a" / "lancedb", root / "b" / "lancedb"
    database = Database.connect(a)
    settings = Settings(
        quarry_root=root, lancedb_path=b, registry_path=root / "b" / "registry.db"
    )
    TableChanges.for_path(b).mark_clean(TableChanges.for_path(b).snapshot())
    job = CollectionFinalizeJob(database, settings, "col")
    loop = asyncio.new_event_loop()
    try:
        job._finalize(loop)
    finally:
        loop.close()
    assert rewrites.optimizes == 1  # A's record was never finalized, so it rewrote


def test_a_retry_is_never_armed_for_less_than_a_second(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "x" / "lancedb"
    settings = Settings(
        quarry_root=tmp_path / "data",
        lancedb_path=path,
        registry_path=tmp_path / "data" / "x" / "registry.db",
    )
    ledger = FinalizeLedgers.for_database(path, settings.quarry_root)
    now = [100.0]
    ledger._clock = lambda: now[0]
    ledger.record_failure("boom")
    now[0] = ledger._retry_at - 0.04
    changes = TableChanges()
    upkeep = TableUpkeep(Database.connect(path).db, settings, changes)
    retry_in = upkeep.retry_in_seconds()
    assert retry_in is not None
    assert retry_in >= 1.0


def test_start_with_data_runs_one_real_compaction_per_database(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("a", "b", "c")
        d.prefill()
        await d.start()
        assert d.rewrites.optimizes == 1
        assert d.rewrites.outcomes[0].optimized is True
        assert d.rewrites.outcomes[0].fts_rebuilt is True
        await d.stop()

    asyncio.run(run())


def test_two_databases_keep_independent_records(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("a", "b", siblings=("other",))
        await d.start()
        assert d.rewrites.optimizes == 2  # one real finalize per database
        d.write_chunk("a")
        await d.safety_scan()
        assert d.rewrites.optimizes == 3  # only the written database rewrote
        await d.stop()

    asyncio.run(run())


def test_a_skipped_sweep_finalize_leaves_the_throttle_clock_and_trailing_alone(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("col", interval=2.0, manual_clock=True)
        assert d.clock is not None
        await d.start()
        throttle = _throttle(d)
        stamp = throttle._last[_DATABASE]
        _arm_trailing_finalize(d)
        await _until(lambda: _armed_trailing(d))
        assert _armed_trailing(d)
        finalizes = d.queue.finalizes
        # Move the clock first, so a stamp written by the skipped sweep reads 1.0
        # and cannot be mistaken for the start finalize's 0.0.
        d.clock.set_time(1.0)
        await d.safety_scan()  # a skipped sweep finalize
        assert d.queue.finalizes == finalizes + 1
        assert throttle._last[_DATABASE] == stamp
        assert _armed_trailing(d)
        # The armed trailing finalize still runs at its due time.
        d.clock.advance(2.0)
        await _until(lambda: d.queue.finalizes == finalizes + 2)
        await d.queue.settled()
        assert d.queue.finalizes == finalizes + 2
        await d.stop()

    asyncio.run(run())


def test_a_throttled_finalize_that_skips_still_stamps_the_clock(
    daemon: Callable[..., _Daemon],
) -> None:
    """The throttle stamps when it ADMITS a finalize, before the job decides to skip.

    The cost is bounded: a write inside the next interval is coalesced behind the
    skipped finalize's stamp, so its rewrite is delayed by at most one interval.
    """

    async def run() -> None:
        d = daemon("col", interval=0.3)
        await d.start()
        throttle = _throttle(d)
        stamp = throttle._last[_DATABASE]  # the start finalize's rewrite stamped it
        rewrites = d.rewrites.optimizes
        _arm_trailing_finalize(d)
        for _ in range(100):  # the trailing finalize fires and skips
            if throttle._last[_DATABASE] > stamp:
                break
            await asyncio.sleep(0.05)
        await d.queue.settled()
        assert throttle._last[_DATABASE] > stamp
        assert d.rewrites.optimizes == rewrites  # and the finalize skipped
        await d.stop()

    asyncio.run(run())


def test_an_unexpected_exception_backs_off_through_the_ledger(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("col", interval=30.0)
        d.rewrites.failures = [TypeError("unexpected")]
        await d.start()
        ledger = FinalizeLedgers.for_database(
            d.settings.lancedb_path, d.settings.quarry_root
        )
        health = ledger.health()
        assert health.consecutive_failures == 1
        assert health.last_error == "TypeError"
        changes = LanceConnection.changes_of(d.database.db)
        upkeep = TableUpkeep(d.database.db, d.settings, changes)
        retry_in = upkeep.retry_in_seconds()
        assert retry_in is not None
        assert retry_in >= 500.0
        await d.stop()

    asyncio.run(run())


def test_a_retry_coalesced_into_the_trailing_timer_is_armed_again_when_shed(
    daemon: Callable[..., _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("quarry.finalize_ledger.BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr("quarry.daemon.finalize_retry.SHED_RETRY_S", 0.1)

    async def run() -> None:
        # The interval is longer than the one second retry, so the retry is
        # coalesced into the throttle's trailing timer, and that fire is shed.
        d = daemon("col", interval=3.0)
        d.rewrites.failures = [RuntimeError("boom")]
        await d.start()
        d.queue.shed = True
        await asyncio.sleep(1.5)
        assert _armed_trailing(d)  # the retry coalesced; no direct submit happened
        await asyncio.sleep(3.0)  # the trailing timer fires into a full queue
        assert d.rewrites.optimizes == 1
        d.queue.shed = False
        await _until(lambda: d.rewrites.optimizes >= 2)
        await d.queue.settled()
        assert d.rewrites.optimizes == 2
        await d.stop()

    asyncio.run(run())


def test_a_closed_loop_at_shutdown_does_not_replace_the_original_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rewrites = _Rewrites(monkeypatch)
    rewrites.failures = [KeyError("original")]
    root = tmp_path / "data"
    settings = Settings(
        quarry_root=root,
        lancedb_path=root / "a" / "lancedb",
        registry_path=root / "a" / "registry.db",
    )
    database = Database.connect(settings.lancedb_path)
    job = CollectionFinalizeJob(
        database, settings, "col", on_unsettled=lambda delay: None
    )
    loop = asyncio.new_event_loop()
    loop.close()
    with pytest.raises(KeyError):
        job._finalize(loop)
    job._finalize(loop)  # a normal return at shutdown must not raise either


def test_a_shed_retry_is_armed_again_when_a_write_armed_the_trailing_timer(
    daemon: Callable[..., _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The throttle keeps the write's closure; the shed must still re-arm the retry."""
    monkeypatch.setattr("quarry.finalize_ledger.BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr("quarry.daemon.finalize_retry.SHED_RETRY_S", 0.1)

    async def run() -> None:
        d = daemon("col", interval=3.0)
        d.rewrites.failures = [RuntimeError("boom")]
        await d.start()  # the failed finalize arms a retry for about a second
        _arm_trailing_finalize(d)  # a write arms the trailing timer first
        await _until(lambda: _armed_trailing(d))
        assert _armed_trailing(d)
        d.queue.shed = True
        await asyncio.sleep(4.5)  # the retry coalesces; the trailing fire is shed
        assert d.rewrites.optimizes == 1
        d.queue.shed = False
        await _until(lambda: d.rewrites.optimizes >= 2)
        await d.queue.settled()
        assert d.rewrites.optimizes == 2
        await d.stop()

    asyncio.run(run())


def test_a_retry_for_a_database_left_the_roster_is_dropped_quietly(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("col", interval=0.05)
        await d.start()
        problems: list[dict[str, object]] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: problems.append(context)
        )
        submitter = d.loop._submitter
        roster = d.loop._roster
        assert submitter is not None
        assert roster is not None
        submitter.retry_finalize(RouteKey(_DATABASE, "col"), 0.05)
        roster._conns.pop(_DATABASE)
        roster._db_settings.pop(_DATABASE)
        await asyncio.sleep(0.4)  # the retry timer fires
        assert problems == []
        await d.stop()

    asyncio.run(run())


def test_a_finalize_does_not_wait_inside_the_gate_for_a_manual_optimize(
    daemon: Callable[..., _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("quarry.table_finalizer.BUSY_RETRY_S", 0.3)

    async def run() -> None:
        d = daemon("col", interval=0.05)
        d.prefill()
        await d.start()
        lock = LanceConnection.changes_of(d.database.db).optimizing()
        held = threading.Event()
        release = threading.Event()

        def hold_lock() -> None:
            with lock:
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        held.wait(5)
        d.write_chunk(name="busy.md")  # the table is dirty
        before = d.rewrites.optimizes
        await asyncio.wait_for(d.safety_scan(), 5)  # returns promptly: no waiting
        assert d.rewrites.optimizes == before
        assert LanceConnection.changes_of(d.database.db).needs_rewrite()
        ledger = FinalizeLedgers.for_database(
            d.settings.lancedb_path, d.settings.quarry_root
        )
        assert ledger.health().consecutive_failures == 0  # busy is not a failure
        release.set()
        holder.join()
        await _until(lambda: d.rewrites.optimizes > before)  # the retry rewrites
        await d.queue.settled()
        assert d.rewrites.optimizes == before + 1
        await d.stop()

    asyncio.run(run())


def test_a_failed_orphan_sweep_is_attempted_again_on_the_next_finalize(
    daemon: Callable[..., _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []

    def flaky_sweep(sweep: OrphanPageSweep, now: datetime | None = None) -> int:
        del sweep, now
        attempts.append(1)
        if len(attempts) == 1:
            msg = "disk"
            raise OSError(msg)
        return 0

    monkeypatch.setattr(OrphanPageSweep, "run", flaky_sweep)

    async def run() -> None:
        d = daemon("col", interval=30.0)
        await d.start()
        assert len(attempts) == 1  # the rewrite's sweep failed
        await d.safety_scan()
        assert len(attempts) == 2  # not marked maintained, so due again
        await d.safety_scan()
        assert len(attempts) == 2  # the successful sweep restarted the clock
        await d.stop()

    asyncio.run(run())


def test_a_failed_manual_rebuild_on_a_registrationless_database_is_retried(
    daemon: Callable[..., _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = [False]
    _fail_fts_while(monkeypatch, failing)
    monkeypatch.setattr("quarry.finalize_ledger.BACKOFF_BASE_SECONDS", 0.0)

    async def run() -> None:
        d = daemon("col", interval=0.05)
        d.prefill()
        registry = SyncRegistry(d.settings.registry_path)
        try:
            registry.deregister_directory("col")
        finally:
            registry.close()
        await d.start()  # no registration, so no finalize at start
        assert d.rewrites.optimizes == 0
        failing[0] = True
        manual = ManualOptimize(d.database, d.settings)
        outcome = manual.run_manually(force=False)
        assert outcome.fts_rebuilt is False
        failing[0] = False
        results = OptimizeReport(outcome, force=False).results()
        assert manual.finalize_retry_delay(results) is not None
        d.loop.arm_finalize_retry(0.05)
        await _until(lambda: d.rewrites.optimizes >= 2)
        await d.queue.settled()
        assert d.rewrites.optimizes == 2  # the manual run, then the finalize
        assert d.rewrites.outcomes[-1].fts_rebuilt is True
        await d.stop()

    asyncio.run(run())


def test_a_finalize_on_another_collection_can_overtake_a_queued_index_job(
    daemon: Callable[..., _Daemon],
) -> None:
    """DES-045c: that write waits for the next safety scan; the lag is not new.

    The interval runs on a manual clock.  On the wall clock the start finalize,
    which really rewrites, can outlast the interval on a loaded machine, and y's
    edit then finalizes at once instead of arming the trailing timer under test.
    """

    async def run() -> None:
        d = daemon("x", "y", interval=0.5, manual_clock=True)
        clock = d.clock
        assert clock is not None
        await d.start()
        base = d.rewrites.optimizes
        d.queue.hold("x")
        for name in ("y", "x"):  # y first: its edit arms the trailing timer
            path = d.roots[name] / f"{name}.md"
            path.write_text("body")
            d.source.emit(d.roots[name], FsEvent(path, deleted=False))
            await _until(partial(d.queue.has_file_job, name))
            assert d.queue.has_file_job(name)
        assert len(clock.live()) == 1  # both edits coalesced into one timer
        finalizes = d.queue.finalizes
        d.queue.on_index = d.write_chunk  # x's job writes when it finally runs
        clock.advance(0.5)  # the trailing timer fires, under y's key
        await _until(lambda: d.queue.finalizes > finalizes)
        assert d.queue.finalizes == finalizes + 1
        assert d.rewrites.optimizes == base  # the finalize ran first and skipped
        d.queue.release("x")
        await d.queue.settled()
        clock.advance(1.2)  # no timer is left to finalize x's write
        await d.queue.settled()
        assert clock.live() == []  # the throttle's trailing and the retry timers
        submitter = d.loop._submitter
        assert submitter is not None
        assert submitter._rearm._timers == set()  # no shed event waits to re-feed
        assert d.queue.finalizes == finalizes + 1
        assert d.rewrites.optimizes == base
        await d.safety_scan()
        assert d.rewrites.optimizes == base + 1
        await d.stop()

    asyncio.run(run())


def test_a_registry_that_will_not_open_still_arms_the_retry(
    daemon: Callable[..., _Daemon],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(_roster: WatchRoster, _name: str) -> list[tuple[str, Path]]:
        msg = "registry is locked"
        raise sqlite3.OperationalError(msg)

    async def run() -> None:
        d = daemon("col", interval=0.05)
        d.prefill()
        await d.start()
        d.write_chunk(name="dirty.md")  # something for the retry to rewrite
        before = d.rewrites.optimizes
        monkeypatch.setattr(WatchRoster, "registrations", broken)  # after the start
        with caplog.at_level("WARNING", logger="quarry.daemon.maintenance_key"):
            d.loop.arm_finalize_retry(0.05)  # must not raise
        fallback = [
            record
            for record in caplog.records
            if record.name == "quarry.daemon.maintenance_key"
            and record.levelname == "WARNING"
            and "__maintenance__" in record.getMessage()
        ]
        assert len(fallback) == 1, caplog.text
        assert "registry is locked" in fallback[0].getMessage()
        await _until(lambda: d.rewrites.optimizes > before)
        await d.queue.settled()
        assert d.rewrites.optimizes == before + 1  # the maintenance key still ran it
        await d.stop()

    asyncio.run(run())
