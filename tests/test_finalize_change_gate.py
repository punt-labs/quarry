"""A finalize rewrites the table only when the table changed since its last one.

Every test drives the real submit path (the safety-scan sweep, a watch batch, an
explicit sync, a daemon start) into a queue that RUNS the admitted finalize jobs
in FIFO order against a real LanceDB database.  "Table-wide work" is counted at
the two calls that rewrite the table: ``TableOptimizer.optimize`` (compaction,
cleanup, full-text rebuild) and ``TableOptimizer.create_collection_index``.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Self, cast, final

import numpy as np
import pytest

from quarry.config import Settings
from quarry.daemon.collection_finalize_job import CollectionFinalizeJob
from quarry.daemon.fs_events import FsEvent
from quarry.daemon.index_jobs import CollectionSyncJob
from quarry.daemon.tasks import TaskRegistry
from quarry.daemon.watch_loop import WatchLoop
from quarry.db import Database, TableOptimizer
from quarry.db.orphan_pages import OrphanPageSweep
from quarry.db.table_changes import TableChanges
from quarry.finalize_ledger import FinalizeLedgers
from quarry.finalize_steps import ShadowPushStep
from quarry.ingestion.pipeline import plan_file_chunks
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.route_key import RouteKey
    from quarry.daemon.task_state import TaskState

_DATABASE = "testdb"


@final
class _Rewrites:
    """Count the calls that rewrite the table, and run a hook inside the rewrite."""

    __slots__ = ("during", "failure", "optimizes", "scalar_indexes")

    optimizes: int
    scalar_indexes: int
    failure: Exception | None
    during: Callable[[], None] | None

    def __new__(cls, monkeypatch: pytest.MonkeyPatch) -> Self:
        self = super().__new__(cls)
        self.optimizes = 0
        self.scalar_indexes = 0
        self.failure = None
        self.during = None
        real_optimize = TableOptimizer.optimize
        real_index = TableOptimizer.create_collection_index

        def optimize(opt: TableOptimizer, *, force: bool = False) -> object:
            self.optimizes += 1
            if self.during is not None:
                self.during()
            if self.failure is not None:
                raise self.failure
            return real_optimize(opt, force=force)

        def index(opt: TableOptimizer) -> None:
            self.scalar_indexes += 1
            real_index(opt)

        monkeypatch.setattr(TableOptimizer, "optimize", optimize)
        monkeypatch.setattr(TableOptimizer, "create_collection_index", index)
        return self


@final
class _ExecutingQueue:
    """Admit every job and run it FIFO: scans via a hook, finalizes for real."""

    __slots__ = ("_ctx", "_pending", "_worker", "on_scan")

    _ctx: DaemonContext | None
    _pending: asyncio.Queue[tuple[IngestUnit, TaskState]] | None
    _worker: asyncio.Task[None] | None
    on_scan: Callable[[], None]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._ctx = None
        self._pending = None
        self._worker = None
        self.on_scan = lambda: None
        return self

    def bind(self, ctx: DaemonContext) -> None:
        self._ctx = ctx

    def try_submit(self, key: RouteKey, job: IngestUnit, state: TaskState) -> bool:
        del key
        if self._pending is None:
            self._pending = asyncio.Queue()
            self._worker = asyncio.get_running_loop().create_task(self._drain())
        self._pending.put_nowait((job, state))
        return True

    async def settled(self) -> None:
        """Wait until every admitted job has run."""
        if self._pending is not None:
            await self._pending.join()

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    async def _drain(self) -> None:
        assert self._pending is not None
        assert self._ctx is not None
        while True:
            job, state = await self._pending.get()
            try:
                if isinstance(job, CollectionFinalizeJob):
                    await job.run(self._ctx, state)
                else:
                    if isinstance(job, CollectionSyncJob):
                        self.on_scan()
                    state.complete({})
            finally:
                self._pending.task_done()


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

    __slots__ = ("collections", "ctx", "loop", "queue", "rewrites", "roots", "source")

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
    ) -> Self:
        self = super().__new__(cls)
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
        await self.loop.stop()
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

    def write_chunk(self, collection: str = "", name: str = "doc.md") -> None:
        """Index one document into *collection* through the real chunk store."""
        collection = collection or self.collections[0]
        path = self.roots[collection] / f".{name}"
        path.write_text("indexable body " * 20)
        chunks, _ = plan_file_chunks(
            path, self.settings, collection=collection, document_name=name
        )
        self.database.store.insert(
            chunks, np.zeros((len(chunks), 768), dtype=np.float32)
        )


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Daemon]:
    def make(*collections: str, bulk: int = 50, interval: float = 0.0) -> _Daemon:
        return _Daemon(
            tmp_path, monkeypatch, collections or ("col",), bulk=bulk, interval=interval
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
        d = daemon("col")
        d.rewrites.failure = RuntimeError("compaction failed")
        await d.start()
        assert d.rewrites.optimizes == 1
        d.rewrites.failure = None
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
        d = daemon("col")
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


def _armed_trailing(d: _Daemon) -> bool:
    submitter = d.loop._submitter
    assert submitter is not None
    return bool(submitter._throttle._timers)


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


def test_rewriting_finalize_cancels_an_armed_trailing_finalize(
    daemon: Callable[..., _Daemon],
) -> None:
    async def run() -> None:
        d = daemon("col", interval=30.0)
        await d.start()
        _arm_trailing_finalize(d)
        await asyncio.sleep(0.15)
        assert _armed_trailing(d)
        d.queue.on_scan = d.write_chunk
        await d.safety_scan()
        assert not _armed_trailing(d)
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
