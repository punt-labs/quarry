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
from quarry.daemon.finalize_job import CollectionFinalizeJob, CollectionPurgeJob
from quarry.daemon.fs_events import FsEvent
from quarry.daemon.index_jobs import CollectionSyncJob, DocumentDeleteJob, FileIndexJob
from quarry.daemon.route_key import RouteKey
from quarry.daemon.routes.registrations import RegistrationRoutes
from quarry.daemon.tasks import TaskRegistry
from quarry.daemon.watch_loop import WatchLoop
from quarry.daemon.watch_reconcile import WatchReconciler
from quarry.daemon.watch_roster import WatchRoster
from quarry.sync_registry import SyncRegistry


def _reconciler(loop: WatchLoop) -> WatchReconciler:
    """Return the started loop's reconciler (present once ``start`` ran)."""
    assert loop._reconciler is not None
    return loop._reconciler


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import pytest

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskState


@final
class _RecordingQueue:
    """Capture every submission; admit or shed per configuration."""

    __slots__ = ("admit", "boom", "fail_index", "scan_result", "submitted")

    submitted: list[tuple[RouteKey, IngestUnit]]
    boom: bool
    admit: bool
    fail_index: bool
    scan_result: dict[str, object]

    def __new__(
        cls,
        *,
        admit: bool = True,
        boom: bool = False,
        fail_index: bool = False,
        scan_result: dict[str, object] | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self.submitted = []
        self.admit = admit
        self.boom = boom
        self.fail_index = fail_index
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
        # Simulate the worker running the admitted job: a CollectionSyncJob
        # reports its injected per-file result; a FileIndexJob fails at run time
        # when fail_index is set (the admitted-then-failed case).
        state.status = "completed"
        if isinstance(job, CollectionSyncJob):
            state.results = dict(self.scan_result)
        elif isinstance(job, FileIndexJob) and self.fail_index:
            state.status = "failed"
            state.error = "index boom"
        return True

    def jobs(self, kind: type) -> list[IngestUnit]:
        """Return every submitted job that is an instance of *kind*."""
        return [job for _key, job in self.submitted if isinstance(job, kind)]


@final
class _FakeSource:
    """A synthetic FsEventSource: record scheduled trees, emit events on demand."""

    __slots__ = ("_watches", "null_handle", "stopped")

    _watches: dict[object, tuple[Path, Callable[[FsEvent], None]]]
    null_handle: bool
    stopped: bool

    def __new__(cls, *, null_handle: bool = False) -> Self:
        self = super().__new__(cls)
        self._watches = {}
        self.null_handle = null_handle
        self.stopped = False
        return self

    def schedule(
        self, root: Path, on_event: Callable[[FsEvent], None]
    ) -> object | None:
        # null_handle mimics inotify ENOSPC: the tree is scheduled but no handle
        # is returned, so the roster records it as watched-but-unobserved.
        handle = None if self.null_handle else object()
        self._watches[handle] = (Path(root), on_event)
        return handle

    def unschedule(self, handle: object | None) -> None:
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


def _write(root: Path, name: str) -> Path:
    """Create an indexable file under *root* (a real modify event implies it exists)."""
    path = root / name
    path.write_text("indexable body text")
    return path


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
        target = _write(root, "a.md")
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
            source.emit(root, _fs(_write(root, f"f{i}.md")))
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
        source.emit(root, _fs(_write(root, "a.md")))
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
        source.emit(root, _fs(_write(root, "a.md")))
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
        _reconciler(loop).run_once()  # the safety scan retries the shed scan
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
        _reconciler(loop).run_once()
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


def test_admitted_then_failed_file_job_is_rescanned(tmp_path: Path) -> None:
    """An admitted per-file job that FAILS at run time is reconciled from disk.

    An admitted job that then fails would otherwise be logged and forgotten. The
    periodic disk-vs-registry reconcile re-scans every registered collection, so
    the failed file's collection is re-synced from disk — never stale-forever.
    """

    async def _run() -> None:
        queue = _RecordingQueue(fail_index=True)  # admitted, then fails at run
        ctx, root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        queue.submitted.clear()
        source.emit(root, _fs(_write(root, "a.md")))  # live edit -> FileIndexJob
        await asyncio.sleep(0.15)
        assert queue.jobs(FileIndexJob)  # admitted (then failed at run)
        queue.submitted.clear()
        queue.fail_index = False  # the rescan will succeed
        _reconciler(loop).run_once()  # full disk-vs-registry pass re-scans
        assert len(queue.jobs(CollectionSyncJob)) == 1
        await loop.stop()

    asyncio.run(_run())


def test_reconcile_scans_a_none_handle_tree(tmp_path: Path) -> None:
    """A tree whose schedule() returns None is still reconciled from disk (#6).

    A None handle (inotify ENOSPC / unwatchable) means no live events arrive, so
    the tree must not be treated as "already watched and fresh" — the periodic
    disk-vs-registry reconcile re-scans it regardless of handle state.
    """

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, _root = _build(tmp_path, queue=queue)
        source = _FakeSource(null_handle=True)  # schedule() returns no handle
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        assert source.watch_count == 1  # scheduled, but with no observer handle
        queue.submitted.clear()
        _reconciler(loop).run_once()
        assert len(queue.jobs(CollectionSyncJob)) == 1  # disk-scanned anyway
        await loop.stop()

    asyncio.run(_run())


def test_start_stop_start_rebuilds_a_live_source(tmp_path: Path) -> None:
    """A second start() after stop() rebuilds fresh, live collaborators.

    stop() joins the observer thread (a joined watchdog observer cannot be
    restarted), so it drops the built collaborators; a subsequent start() must
    reconstruct them, not silently reuse a dead observer that watches nothing.
    """

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, root = _build(tmp_path, queue=queue)
        loop = WatchLoop(ctx, source=_FakeSource())
        await loop.start()
        await loop.stop()
        # The built collaborators are dropped so a restart can't reuse a joined
        # (dead) observer that would watch nothing.
        assert loop._source is None

        # Restart with a fresh source (production start() builds a new observer);
        # the rebuilt loop must deliver a live edit as a job.
        source = _FakeSource()
        loop._source = source
        await loop.start()
        queue.submitted.clear()
        source.emit(root, _fs(_write(root, "z.md")))
        await asyncio.sleep(0.15)
        assert queue.jobs(FileIndexJob)  # the rebuilt loop is live
        await loop.stop()

    asyncio.run(_run())


def test_start_survives_unreadable_roster_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable quarry_root does not crash boot; the active DB still watches.

    roster_names() iterates quarry_root to find sibling databases; an OSError
    there must fall back to the active database, never bring down daemon boot.
    """

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, _root = _build(tmp_path, queue=queue)
        real_iterdir = Path.iterdir

        def boom(self: Path) -> Iterator[Path]:
            if self == ctx.settings.quarry_root:
                msg = "permission denied"
                raise OSError(msg)
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", boom)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()  # must not raise
        assert source.watch_count == 1  # the active DB's collection still watched
        await loop.stop()

    asyncio.run(_run())


def test_reconcile_drops_a_watch_whose_registration_was_removed(tmp_path: Path) -> None:
    """A registration removed from disk has its watch torn down on reconcile (#2).

    A removed/renamed directory fires no delete event, so without this the
    observer for the gone collection would persist forever.
    """

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, _root = _build(tmp_path, queue=queue)  # registers "col"
        extra = tmp_path / "proj2"
        extra.mkdir()
        _register(ctx.settings, extra.resolve(), "col2")
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        assert source.watch_count == 2  # both collections watched

        conn = SyncRegistry(ctx.settings.registry_path)
        try:
            conn.deregister_directory("col2")  # remove col2's registration
        finally:
            conn.close()
        _reconciler(loop).run_once()
        assert source.watch_count == 1  # col2's observer was torn down
        await loop.stop()

    asyncio.run(_run())


def test_symlink_escaping_the_tree_is_never_submitted(tmp_path: Path) -> None:
    """A symlink whose target escapes the watched root is not indexed (security).

    The cheap observer-thread pre-filter no longer resolves; the authoritative
    symlink-escape check runs post-debounce in the submitter BEFORE any content
    is read.  A symlink pointing outside the tree must produce no FileIndexJob.
    """

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, root = _build(tmp_path, queue=queue)
        secret = tmp_path / "outside" / "secret.md"
        secret.parent.mkdir()
        secret.write_text("private", encoding="utf-8")
        escape = root / "escape.md"
        escape.symlink_to(secret)  # target resolves outside the watched root
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        queue.submitted.clear()
        source.emit(root, _fs(escape))  # live edit of the escaping symlink
        await asyncio.sleep(0.15)
        assert not queue.jobs(FileIndexJob)  # rejected before any content read
        await loop.stop()

    asyncio.run(_run())


def _reregister_setup(
    tmp_path: Path, *, queue: _RecordingQueue, collection: str = "docs"
) -> tuple[WatchLoop, DaemonContext, _FakeSource, Path]:
    """Register /root/sub as *collection*; return the loop, ctx, source, /root.

    /root is a strict parent of /root/sub, so registering /root subsumes the
    child.  Same *collection* on both makes it a self-subsume (same-name
    re-registration); a different parent name makes the child a distinct
    subsumed collection.
    """
    data = tmp_path / "data"
    (data / "testdb").mkdir(parents=True, exist_ok=True)
    root = tmp_path / "proj"
    sub = root / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        quarry_root=data,
        lancedb_path=data / "testdb" / "lancedb",
        registry_path=data / "testdb" / "registry.db",
        watch_enabled=True,
        watch_debounce_s=0.03,
        watch_max_delay_s=0.3,
        watch_bulk_threshold=5,
        watch_safety_scan_s=0.0,  # drive _reconcile directly; no background timer
    )
    _register(settings, sub.resolve(), collection)
    ns = SimpleNamespace(
        settings=settings,
        ingest_queue=queue,
        tasks=TaskRegistry(),
        database=object(),
        database_name="testdb",
    )
    source = _FakeSource()
    loop = WatchLoop(cast("DaemonContext", ns), source=source)
    ns.watch_loop = loop  # RegistrationRoutes reaches ctx.watch_loop
    return loop, cast("DaemonContext", ns), source, root


def test_same_name_reregistration_purges_before_rewatch(tmp_path: Path) -> None:
    """Re-registering "docs" under a wider dir purges old chunks, THEN re-watches.

    directories.collection is UNIQUE, so registering /root as "docs" when
    /root/sub is already "docs" subsumes "docs" itself.  _run_register must purge
    the stale (sub-relative) chunks and only THEN install the parent watch + scan
    — never tear the freshly installed watch down or purge its scan.  Proven by
    the job order (the purge precedes the re-install scan) and the surviving watch.
    """

    async def _run() -> None:
        queue = _RecordingQueue()
        loop, ctx, source, root = _reregister_setup(tmp_path, queue=queue)
        await loop.start()
        assert source.watch_count == 1  # /root/sub watched as "docs"
        queue.submitted.clear()

        key = RouteKey("testdb", "docs")
        reg = ctx.tasks.begin("register")
        await RegistrationRoutes(ctx)._run_register(reg, root.resolve(), "docs")
        assert reg.status == "completed"
        assert reg.results["subsumed"] == ["docs"]  # subsumed its own name
        # "docs" is watched again — re-installed at the parent, not torn down.
        assert source.watch_count == 1
        # Job order on "docs": the purge precedes the re-install scan, so the
        # scan's chunks survive (no orphan, no scan-then-purge emptiness).
        docs_jobs = [job for k, job in queue.submitted if k == key]
        purge_idx = next(
            i for i, j in enumerate(docs_jobs) if isinstance(j, CollectionPurgeJob)
        )
        assert any(isinstance(j, CollectionSyncJob) for j in docs_jobs[purge_idx + 1 :])
        await loop.stop()

    asyncio.run(_run())


def test_failed_subsume_purge_is_retried_on_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A distinct subsumed child's failed purge is retried until reconcile admits it.

    Registering /root as "parent" over /root/sub "child" subsumes "child" (a
    distinct collection that does NOT become live).  Its purge fails on a full
    queue, so it is deferred and re-submitted on the next reconcile; once the
    queue admits it the delete runs and the pending entry clears.
    """
    # A zero admission deadline makes the register-time purge fail immediately.
    monkeypatch.setattr("quarry.daemon.purge_service._PURGE_SUBMIT_DEADLINE_S", 0.0)

    async def _run() -> None:
        queue = _RecordingQueue()
        loop, ctx, _source, root = _reregister_setup(
            tmp_path, queue=queue, collection="child"
        )
        await loop.start()
        child = RouteKey("testdb", "child")
        # The queue is full: registering the parent subsumes "child" but its purge
        # cannot be admitted, so the child is deferred, never silently dropped.
        queue.admit = False
        reg = ctx.tasks.begin("register")
        await RegistrationRoutes(ctx)._run_register(reg, root.resolve(), "parent")
        assert reg.results["subsume_purge_failed"] == ["child"]
        assert loop._submitter is not None
        assert child in _reconciler(loop)._pending_purges
        # The queue drains; the next reconcile re-submits the purge and it lands
        # ("child" is absent from the roster, so the drain purges it).
        queue.admit = True
        queue.submitted.clear()
        _reconciler(loop).run_once()
        assert not _reconciler(loop)._pending_purges  # retry succeeded, entry cleared
        assert queue.jobs(CollectionPurgeJob)  # a fresh purge was submitted
        await loop.stop()

    asyncio.run(_run())


def test_reconcile_drain_skips_live_collections_and_purges_absent_ones(
    tmp_path: Path,
) -> None:
    """A re-registered (live) collection supersedes its stale deferred purge.

    A same-name re-register can leave a collection both live and in the pending
    set.  The drain must skip a live key — purging it would wipe the re-created
    collection's fresh chunks — while still purging a key absent from the roster.
    """

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, _root = _build(tmp_path, queue=queue)  # registers "col" (live)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        assert loop._submitter is not None
        live = RouteKey("testdb", "col")  # registered → live in the roster
        gone = RouteKey("testdb", "gone")  # never registered → absent
        _reconciler(loop).defer_purge(live)
        _reconciler(loop).defer_purge(gone)
        queue.submitted.clear()
        _reconciler(loop).run_once()
        purged = {k for k, j in queue.submitted if isinstance(j, CollectionPurgeJob)}
        assert live not in purged  # never wipe a live collection
        assert gone in purged  # a genuinely absent orphan is still purged
        assert not _reconciler(loop)._pending_purges  # both resolved
        await loop.stop()

    asyncio.run(_run())


def test_begin_collection_cancels_a_stale_deferred_purge(tmp_path: Path) -> None:
    """Beginning a watch for a collection cancels any stale deferred purge at once.

    Defense-in-depth: a re-registration supersedes an orphan-purge immediately,
    not only at the next reconcile, so a mid-window drain can never wipe it.
    """

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        assert loop._submitter is not None
        extra = RouteKey("testdb", "extra")
        _reconciler(loop).defer_purge(extra)
        loop.start_watching("extra", root)  # re-registration makes it live
        assert extra not in _reconciler(loop)._pending_purges  # cancelled at once
        await loop.stop()

    asyncio.run(_run())


def test_register_status_completed_only_after_watch_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll never sees 'completed' before the parent watch is installed.

    The terminal status is set at the END of _run_register, so at the instant
    start_watching runs the task is still 'running'.  The old order set
    'completed' first, exposing an unwatched-but-completed window to a client.
    """
    status_at_watch: list[str] = []
    reg_holder: list[TaskState] = []
    original = WatchLoop.start_watching

    def spy(self: WatchLoop, collection: str, resolved: Path) -> None:
        status_at_watch.append(reg_holder[0].status)
        original(self, collection, resolved)

    monkeypatch.setattr(WatchLoop, "start_watching", spy)

    async def _run() -> None:
        queue = _RecordingQueue()
        loop, ctx, _source, _root = _reregister_setup(tmp_path, queue=queue)
        await loop.start()
        reg = ctx.tasks.begin("register")
        reg_holder.append(reg)
        fresh = tmp_path / "fresh"  # unrelated tree — a clean register, no subsume
        fresh.mkdir()
        await RegistrationRoutes(ctx)._run_register(reg, fresh.resolve(), "newcol")
        assert status_at_watch == ["running"]  # not completed when the watch installs
        assert reg.status == "completed"  # completed only after the watch is live
        await loop.stop()

    asyncio.run(_run())


def test_partial_reconcile_does_not_tear_down_or_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconcile whose enumeration raises partway drives NO removal actions.

    With `current` only partially built, tearing down `watched - current` or
    draining purges by `current` could destroy a live watch or its chunks — so
    both removal actions wait for a fully-successful enumeration.
    """

    async def _run() -> None:
        queue = _RecordingQueue()
        ctx, _root = _build(tmp_path, queue=queue)  # registers "col"
        extra = tmp_path / "proj2"
        extra.mkdir()
        _register(ctx.settings, extra.resolve(), "col2")
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        assert source.watch_count == 2  # both collections watched
        assert loop._submitter is not None
        gone = RouteKey("testdb", "gone")
        _reconciler(loop).defer_purge(gone)  # an absent orphan awaiting purge
        queue.submitted.clear()

        # Enumeration now raises → the reconcile cannot complete.
        def boom(self: WatchRoster, name: str) -> list[tuple[str, Path]]:
            raise OSError("registry read failed")

        monkeypatch.setattr(WatchRoster, "registrations", boom)
        _reconciler(loop).run_once()
        # No live watch torn down; no purge drained this cycle.
        assert source.watch_count == 2
        assert gone in _reconciler(loop)._pending_purges
        assert not queue.jobs(CollectionPurgeJob)
        await loop.stop()

    asyncio.run(_run())


def test_failed_deregister_purge_is_retried_on_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deregister-purge the full queue rejects is deferred and retried.

    Symmetric with the subsume path (the z-spec I2 fix): without the deferral a
    shed deregister-purge would orphan the collection's chunks with no backstop —
    its registry rows are gone, so a plain reconcile would never revisit it.
    """
    monkeypatch.setattr("quarry.daemon.purge_service._PURGE_SUBMIT_DEADLINE_S", 0.0)

    async def _run() -> None:
        queue = _RecordingQueue()
        loop, ctx, _source, _root = _reregister_setup(
            tmp_path, queue=queue, collection="col"
        )
        await loop.start()
        assert loop._submitter is not None
        col = RouteKey("testdb", "col")
        # Deregister "col": the registry rows are removed, then the purge sheds.
        conn = SyncRegistry(ctx.settings.registry_path)
        try:
            conn.deregister_directory("col")
        finally:
            conn.close()
        queue.admit = False
        purge_state = ctx.tasks.begin("deregister")
        purge_state.results = {"deleted_chunks": 0}
        await RegistrationRoutes(ctx)._run_purge(purge_state, "col")
        assert purge_state.status == "failed"
        assert col in _reconciler(loop)._pending_purges  # deferred, not orphaned
        # The queue drains; the next reconcile re-submits the purge ("col" is
        # absent from the roster, so the drain purges it).
        queue.admit = True
        queue.submitted.clear()
        _reconciler(loop).run_once()
        assert col not in _reconciler(loop)._pending_purges
        assert queue.jobs(CollectionPurgeJob)
        await loop.stop()

    asyncio.run(_run())


def test_self_subsume_with_shed_purge_stays_live_and_unqueued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-name re-register whose subsume-purge sheds keeps the parent live.

    The compound path: re-registering "docs" to a wider root while the queue is
    full sheds the subsume-purge, but discard-on-begin supersedes the stale
    purge — so the now-live parent is watched and NOT queued for a purge that
    would wipe its fresh chunks.
    """
    monkeypatch.setattr("quarry.daemon.purge_service._PURGE_SUBMIT_DEADLINE_S", 0.0)

    async def _run() -> None:
        queue = _RecordingQueue()
        loop, ctx, source, root = _reregister_setup(tmp_path, queue=queue)  # "docs"
        await loop.start()
        assert source.watch_count == 1
        key = RouteKey("testdb", "docs")
        queue.admit = False  # the subsume-purge will shed
        reg = ctx.tasks.begin("register")
        await RegistrationRoutes(ctx)._run_register(reg, root.resolve(), "docs")
        assert loop._submitter is not None
        # discard-on-begin fired: the now-live parent is not queued for a purge.
        assert key not in _reconciler(loop)._pending_purges
        assert source.watch_count == 1  # and it is watched
        assert reg.status == "completed"
        await loop.stop()

    asyncio.run(_run())


def test_reconcile_drain_reshed_keeps_pending(tmp_path: Path) -> None:
    """A drain re-submission that is itself shed keeps the orphan pending.

    The queue is still full when the reconcile drain retries, so the purge sheds
    again; the entry must survive in the pending set for the next cycle (I2 stays
    satisfied — the orphan stays tracked, never lost).
    """

    async def _run() -> None:
        queue = _RecordingQueue(admit=False)  # full throughout
        ctx, _root = _build(tmp_path, queue=queue)
        source = _FakeSource()
        loop = WatchLoop(ctx, source=source)
        await loop.start()
        assert loop._submitter is not None
        gone = RouteKey("testdb", "gone")
        _reconciler(loop).defer_purge(gone)
        _reconciler(loop).run_once()  # drain re-submits, but the full queue sheds
        assert gone in _reconciler(loop)._pending_purges  # survives the next cycle
        await loop.stop()

    asyncio.run(_run())
