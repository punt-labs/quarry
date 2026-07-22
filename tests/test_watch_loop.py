"""Hermetic tests for the WatchLoop producer (DES-045).

The loop is driven by a synthetic :class:`FsEventSource` and a recording queue —
no watchdog, no real ONNX, no LanceDB writes — so debounce coalescing, the bulk
threshold, delete routing, register/deregister ordering, the single-writer
route key, and the never-crash-the-loop contract are asserted deterministically.
The recording queue captures the ``(RouteKey, IngestUnit)`` submissions the loop
would hand to the real DES-042 queue.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Self, cast, final

from quarry.config import Settings
from quarry.daemon.finalize_job import CollectionFinalizeJob
from quarry.daemon.fs_events import FsEvent
from quarry.daemon.index_jobs import CollectionSyncJob, DocumentDeleteJob, FileIndexJob
from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import TaskRegistry
from quarry.daemon.watch_loop import WatchLoop
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskState


@final
class _RecordingQueue:
    """Capture every submission; admit or shed per configuration."""

    __slots__ = ("admit", "boom", "scan_result", "submitted")

    submitted: list[tuple[RouteKey, IngestUnit]]
    boom: bool
    admit: bool
    scan_result: dict[str, object]

    def __new__(
        cls,
        *,
        admit: bool = True,
        boom: bool = False,
        scan_result: dict[str, object] | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self.submitted = []
        self.admit = admit
        self.boom = boom
        self.scan_result = scan_result or {}
        return self

    def try_submit(self, key: RouteKey, job: IngestUnit, state: TaskState) -> bool:
        if self.boom:
            msg = "queue boom"
            raise RuntimeError(msg)
        self.submitted.append((key, job))
        if not self.admit:
            state.status = "failed"
            return False
        # Simulate the worker running the job to completion; a CollectionSyncJob
        # reports the injected per-file result (failed/errors) it would produce.
        state.status = "completed"
        if isinstance(job, CollectionSyncJob):
            state.results = dict(self.scan_result)
        return True

    def jobs(self, kind: type) -> list[IngestUnit]:
        """Return every submitted job that is an instance of *kind*."""
        return [job for _key, job in self.submitted if isinstance(job, kind)]


@final
class _FakeSource:
    """A synthetic FsEventSource: record scheduled trees, emit events on demand."""

    __slots__ = ("_watches", "stopped")

    _watches: dict[object, tuple[Path, Callable[[FsEvent], None]]]
    stopped: bool

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._watches = {}
        self.stopped = False
        return self

    def schedule(self, root: Path, on_event: Callable[[FsEvent], None]) -> object:
        handle = object()
        self._watches[handle] = (Path(root), on_event)
        return handle

    def unschedule(self, handle: object) -> None:
        self._watches.pop(handle, None)

    def stop(self) -> None:
        self.stopped = True

    def emit(self, root: Path, event: FsEvent) -> None:
        """Fire *event* to every handler watching *root*."""
        for watched_root, on_event in list(self._watches.values()):
            if watched_root == Path(root):
                on_event(event)

    @property
    def watch_count(self) -> int:
        return len(self._watches)


def _register(settings: Settings, directory: Path, collection: str) -> None:
    conn = SyncRegistry(settings.registry_path)
    try:
        conn.register_directory(directory, collection)
    finally:
        conn.close()


def _build(
    tmp_path: Path, *, queue: _RecordingQueue, bulk: int = 5, enabled: bool = True
) -> tuple[DaemonContext, Path]:
    """Return a fake daemon context and the resolved watched root for 'col'."""
    data = tmp_path / "data"
    (data / "testdb").mkdir(parents=True)
    watched = tmp_path / "proj"
    watched.mkdir()
    settings = Settings(
        quarry_root=data,
        lancedb_path=data / "testdb" / "lancedb",
        registry_path=data / "testdb" / "registry.db",
        watch_enabled=enabled,
        watch_debounce_s=0.03,
        watch_max_delay_s=0.3,
        watch_bulk_threshold=bulk,
        watch_safety_scan_s=0.0,  # drive _reconcile directly; no background timer
    )
    _register(settings, watched.resolve(), "col")
    ctx = SimpleNamespace(
        settings=settings,
        ingest_queue=queue,
        tasks=TaskRegistry(),
        database=object(),
        database_name="testdb",
    )
    return cast("DaemonContext", ctx), watched.resolve()


def _fs(path: Path, *, deleted: bool = False) -> FsEvent:
    return FsEvent(path, deleted=deleted)


def test_start_submits_initial_scan_and_finalize_per_collection(tmp_path: Path) -> None:
    """On start, each registered collection gets a bulk scan + a finalize."""

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, _root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        assert len(queue.jobs(CollectionSyncJob)) == 1
        assert len(queue.jobs(CollectionFinalizeJob)) == 1
        assert source.watch_count == 1
        await loop.stop()

    asyncio.run(_run())


def test_ten_edits_coalesce_to_one_file_index_job(tmp_path: Path) -> None:
    """Ten modify events for one file submit exactly one FileIndexJob."""

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        queue.submitted.clear()
        target = root / "a.md"
        for _ in range(10):
            source.emit(root, _fs(target))
        await asyncio.sleep(0.15)
        index_jobs = queue.jobs(FileIndexJob)
        assert len(index_jobs) == 1
        assert cast("FileIndexJob", index_jobs[0]).path == target
        # Quiescence submits exactly one coalesced finalize behind the file job.
        assert len(queue.jobs(CollectionFinalizeJob)) == 1
        await loop.stop()

    asyncio.run(_run())


def test_burst_above_threshold_collapses_to_one_scan(tmp_path: Path) -> None:
    """More than watch_bulk_threshold changed files submit one CollectionSyncJob."""

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, root = _build(tmp_path, queue=queue, bulk=5)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        queue.submitted.clear()
        for i in range(6):
            source.emit(root, _fs(root / f"f{i}.md"))
        await asyncio.sleep(0.15)
        assert len(queue.jobs(CollectionSyncJob)) == 1
        assert queue.jobs(FileIndexJob) == []

    asyncio.run(_run())


def test_delete_event_submits_document_delete_job(tmp_path: Path) -> None:
    """A removed file submits a DocumentDeleteJob for its registry document name."""

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        queue.submitted.clear()
        source.emit(root, _fs(root / "gone.md", deleted=True))
        await asyncio.sleep(0.15)
        deletes = queue.jobs(DocumentDeleteJob)
        assert len(deletes) == 1
        assert cast("DocumentDeleteJob", deletes[0]).documents == ("gone.md",)

    asyncio.run(_run())


def test_all_submissions_route_to_one_key_per_collection(tmp_path: Path) -> None:
    """Every submission for a collection shares one (database, collection) key.

    The single-writer-per-table invariant is the queue's, but it holds only if
    the producer routes every job for a table to the same key — asserted here.
    """

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        for i in range(3):
            source.emit(root, _fs(root / f"e{i}.md"))
        await asyncio.sleep(0.15)
        keys = {key for key, _job in queue.submitted}
        assert keys == {RouteKey("testdb", "col")}
        await loop.stop()

    asyncio.run(_run())


def test_register_then_deregister_starts_then_stops_watching(tmp_path: Path) -> None:
    """start_watching schedules + scans; stop_watching unwatches and drops pending."""

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, _root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()

        extra = tmp_path / "proj2"
        extra.mkdir()
        loop.start_watching("col2", extra.resolve())
        assert source.watch_count == 2  # the new tree is now watched
        assert len(queue.jobs(CollectionSyncJob)) == 2  # initial scan for col2

        queue.submitted.clear()
        loop.stop_watching("col2")
        assert source.watch_count == 1  # col2 unscheduled
        source.emit(extra.resolve(), _fs(extra.resolve() / "x.md"))
        await asyncio.sleep(0.1)
        assert queue.submitted == []  # no job after deregister
        await loop.stop()

    asyncio.run(_run())


def test_shed_submit_defers_and_skips_finalize(tmp_path: Path) -> None:
    """A queue-full (503) submit is not finalized — it re-arms, never forgets."""

    async def _run() -> None:
        queue = _RecordingQueue(admit=False)
        ctx, root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        queue.submitted.clear()
        source.emit(root, _fs(root / "a.md"))
        await asyncio.sleep(0.15)
        # The file job was attempted but shed; NO finalize follows a shed batch,
        # so a full queue never coalesces-and-forgets a still-stale index.
        assert len(queue.jobs(FileIndexJob)) == 1
        assert queue.jobs(CollectionFinalizeJob) == []
        await loop.stop()

    asyncio.run(_run())


def test_raising_queue_never_crashes_the_loop(tmp_path: Path) -> None:
    """A submit that raises is swallowed; the loop keeps handling later events."""

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        queue.boom = True  # the next submit (from the debounce sink) raises
        source.emit(root, _fs(root / "a.md"))
        await asyncio.sleep(0.1)  # the batch sink raises inside _flush, is caught
        # The loop survived: stop() runs cleanly and the observer is torn down.
        await loop.stop()
        assert source.stopped is True

    asyncio.run(_run())


def test_stop_tears_down_the_source(tmp_path: Path) -> None:
    """stop() stops the observer source and is safe to call once started."""

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, _root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        await loop.stop()
        assert source.stopped is True

    asyncio.run(_run())


def test_request_scan_fails_umbrella_when_a_child_is_shed(tmp_path: Path) -> None:
    """An explicit sync whose child scans are shed (503) fails the umbrella task.

    request_scan must reflect the children — a shed/failed child fails the
    umbrella with a count — never report silent success while collections go
    unindexed (DES-045 djb fix 2).
    """

    async def _run() -> None:
        queue = _RecordingQueue(admit=False)  # every submit is shed
        ctx, _root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        umbrella = ctx.tasks.begin("sync")
        await loop.request_scan(umbrella)
        assert umbrella.status == "failed"
        assert umbrella.results["shed"]  # a non-zero shed-job count
        await loop.stop()

    asyncio.run(_run())


def test_request_scan_reports_per_file_failures(tmp_path: Path) -> None:
    """A scan that completes with failed files fails the umbrella (fix 1).

    A CollectionSyncJob completes even when N files failed to index (it records
    ``failed``/``errors`` in its own state); request_scan must roll those up so
    an explicit ``quarry sync`` never reports silent success.
    """

    async def _run() -> None:
        queue = _RecordingQueue(
            scan_result={"failed": 2, "errors": ["a.md: boom", "b.md: boom"]}
        )
        ctx, _root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        umbrella = ctx.tasks.begin("sync")
        await loop.request_scan(umbrella)
        assert umbrella.status == "failed"
        assert umbrella.results["failed"] == 2  # aggregated per-file failures
        assert umbrella.results["errors"] == ["a.md: boom", "b.md: boom"]
        await loop.stop()

    asyncio.run(_run())


def test_safety_scan_retries_a_shed_bulk_scan(tmp_path: Path) -> None:
    """A bulk scan shed by a full queue is re-submitted by the reconcile (fix 3)."""

    async def _run() -> None:
        queue = _RecordingQueue(admit=False)  # initial scan is shed on start
        ctx, _root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        assert queue.jobs(CollectionSyncJob)  # attempted but shed
        queue.submitted.clear()
        queue.admit = True  # queue drained
        loop._reconcile()  # the safety scan retries the shed scan
        assert len(queue.jobs(CollectionSyncJob)) == 1
        await loop.stop()

    asyncio.run(_run())


def test_safety_scan_picks_up_a_collection_registered_after_start(
    tmp_path: Path,
) -> None:
    """A collection registered since start() is watched + scanned by the reconcile.

    Stands in for a sibling database (or collection) created via the CLI while
    the daemon runs — the backstop that retires quarry-uae (fix 3).
    """

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, _root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        queue.submitted.clear()
        # register a new collection after start(), then reconcile
        extra = tmp_path / "proj2"
        extra.mkdir()
        _register(ctx.settings, extra.resolve(), "col2")
        loop._reconcile()
        scans = [j for _key, j in queue.submitted if isinstance(j, CollectionSyncJob)]
        assert any(job.collection == "col2" for job in scans)
        assert source.watch_count == 2  # the new tree is now watched
        await loop.stop()

    asyncio.run(_run())


def test_explicit_sync_runs_with_observer_disabled(tmp_path: Path) -> None:
    """`quarry sync` enqueues real scans even when watch_enabled=false (conf-80).

    watch_enabled gates the always-on observer, not on-demand sync: the queue is
    always up.  A disabled loop schedules no trees but must still run an explicit
    sync, not report success while indexing nothing.
    """

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, _root = _build(tmp_path, queue=queue, enabled=False)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        assert source.watch_count == 0  # observer disabled: nothing watched
        assert queue.submitted == []  # no initial scan on start when disabled
        umbrella = ctx.tasks.begin("sync")
        await loop.request_scan(umbrella)
        # the registered collection was really scanned, and status is truthful
        assert len(queue.jobs(CollectionSyncJob)) == 1
        assert umbrella.status == "completed"
        assert umbrella.results["collections"] == 1
        await loop.stop()

    asyncio.run(_run())
