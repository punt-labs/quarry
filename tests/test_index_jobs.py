"""Tests for the watch loop's index jobs and the SingleFileIndexer core (DES-045).

A real (tmp) LanceDB + registry exercise the jobs end-to-end with a hermetic
embedder (no ONNX).  Covers the DES-034 single-file core, the three job shapes'
task-state contracts, and bug-class 1 (a file that vanished between the fs-event
and the run is a graceful per-file error, never a crash).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast, final
from unittest.mock import patch

import numpy as np

from quarry.config import Settings
from quarry.daemon.context import DaemonContext
from quarry.daemon.index_jobs import CollectionSyncJob, DocumentDeleteJob, FileIndexJob
from quarry.daemon.ingest_queue import IngestQueue
from quarry.daemon.registration_lifecycle import RegistrationLifecycle
from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import TaskState
from quarry.db import Database
from quarry.ingestion.file_indexer import SingleFileIndexer
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    import pytest
    from numpy.typing import NDArray

_DIM = 768


@final
class _FakeEmbedder:
    """A hermetic embedder: random vectors, no ONNX."""

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    @property
    def dimension(self) -> int:
        return _DIM

    @property
    def model_name(self) -> str:
        return "index-jobs-fake"

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        rng = np.random.default_rng(0)
        return rng.standard_normal((len(texts), _DIM)).astype(np.float32)

    def embed_query(self, query: str) -> NDArray[np.float32]:
        vector: NDArray[np.float32] = np.zeros(_DIM, dtype=np.float32)
        return vector


def _fixture(tmp_path: Path) -> tuple[Database, Settings, Path]:
    """Build a registered tmp database and its watched root."""
    base = tmp_path / "data" / "testdb"
    (base / "lancedb").mkdir(parents=True)
    root = tmp_path / "proj"
    root.mkdir()
    settings = Settings.load().resolve_db_paths(None)
    settings = settings.model_copy(
        update={
            "lancedb_path": base / "lancedb",
            "registry_path": base / "registry.db",
        }
    )
    conn = SyncRegistry(settings.registry_path)
    try:
        conn.register_directory(root.resolve(), "col")
    finally:
        conn.close()
    return Database.connect(base / "lancedb"), settings, root.resolve()


def _docs(db: Database) -> set[str]:
    return {d["document_name"] for d in db.catalog.list_documents()}


def _dummy_ctx() -> DaemonContext:
    return cast("DaemonContext", object())


def test_single_file_indexer_indexes_a_file(tmp_path: Path) -> None:
    """index_one ingests a file's chunks and reports them, no error."""
    db, settings, root = _fixture(tmp_path)
    (root / "a.md").write_text("# Title\n\nsome indexable body text here")
    with patch(
        "quarry.ingestion.streaming.get_embedding_backend", return_value=_FakeEmbedder()
    ):
        conn = SyncRegistry(settings.registry_path)
        try:
            outcome = SingleFileIndexer(
                db.store, conn, settings, collection="col", resolved=root
            ).index_one(root / "a.md")
        finally:
            conn.close()
    assert outcome.error is None
    assert outcome.ingested >= 1
    assert "a.md" in _docs(db)


def test_index_one_missing_file_is_graceful(tmp_path: Path) -> None:
    """A file that vanished before the run is a soft error, never a crash (class 1)."""
    db, settings, root = _fixture(tmp_path)
    conn = SyncRegistry(settings.registry_path)
    try:
        outcome = SingleFileIndexer(
            db.store, conn, settings, collection="col", resolved=root
        ).index_one(root / "never_existed.md")
    finally:
        conn.close()
    assert outcome.error is not None
    assert outcome.ingested == 0
    assert "never_existed.md" in outcome.error


def test_index_one_mid_stream_error_is_graceful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recoverable error mid-stream is a soft failure outcome, never a raise."""
    db, settings, root = _fixture(tmp_path)
    (root / "d.md").write_text("body text that would index")

    def _boom(_self: object, _chunks: object, *, start_index: int = 0) -> object:
        del start_index
        msg = "stream boom"
        raise OSError(msg)

    monkeypatch.setattr(
        "quarry.ingestion.streaming.DocumentStreamer.stream_batches", _boom
    )
    with patch(
        "quarry.ingestion.streaming.get_embedding_backend", return_value=_FakeEmbedder()
    ):
        conn = SyncRegistry(settings.registry_path)
        try:
            outcome = SingleFileIndexer(
                db.store, conn, settings, collection="col", resolved=root
            ).index_one(root / "d.md")
        finally:
            conn.close()
    assert outcome.error is not None
    assert "stream boom" in outcome.error


def test_file_index_job_run_completes(tmp_path: Path) -> None:
    """FileIndexJob.run indexes off-thread and records completion + count."""
    db, settings, root = _fixture(tmp_path)
    (root / "b.md").write_text("chunk one body\n\nchunk two body")
    state = TaskState(task_id="idx", kind="index")
    job = FileIndexJob(db, settings, "col", root, root / "b.md")
    with patch(
        "quarry.ingestion.streaming.get_embedding_backend", return_value=_FakeEmbedder()
    ):
        asyncio.run(job.run(_dummy_ctx(), state))
    assert state.status == "completed"
    assert state.results["document"] == "b.md"
    assert cast("int", state.results["ingested"]) >= 1


def test_document_delete_job_removes_chunks_and_rows(tmp_path: Path) -> None:
    """DocumentDeleteJob drops a document's chunks and its registry rows."""
    db, settings, root = _fixture(tmp_path)
    (root / "c.md").write_text("deletable body text")
    with patch(
        "quarry.ingestion.streaming.get_embedding_backend", return_value=_FakeEmbedder()
    ):
        conn = SyncRegistry(settings.registry_path)
        try:
            SingleFileIndexer(
                db.store, conn, settings, collection="col", resolved=root
            ).index_one(root / "c.md")
        finally:
            conn.close()
    assert "c.md" in _docs(db)

    state = TaskState(task_id="del", kind="delete")
    job = DocumentDeleteJob(db, settings, "col", ("c.md",))
    asyncio.run(job.run(_dummy_ctx(), state))
    assert state.status == "completed"
    assert "c.md" not in _docs(db)
    conn = SyncRegistry(settings.registry_path)
    try:
        assert all(rec.document_name != "c.md" for rec in conn.files.list_files("col"))
    finally:
        conn.close()


def test_collection_sync_job_bulk_indexes_all_files(tmp_path: Path) -> None:
    """CollectionSyncJob runs the bulk DES-034 scan over every file in the tree."""
    db, settings, root = _fixture(tmp_path)
    (root / "one.md").write_text("first document body")
    (root / "two.md").write_text("second document body")
    state = TaskState(task_id="scan", kind="sync")
    job = CollectionSyncJob(db, settings, "col", root)
    with patch(
        "quarry.ingestion.streaming.get_embedding_backend", return_value=_FakeEmbedder()
    ):
        asyncio.run(job.run(_dummy_ctx(), state))
    assert state.status == "completed"
    assert state.results["ingested"] == 2
    assert _docs(db) == {"one.md", "two.md"}


def test_collection_sync_job_deletes_documents_gone_from_disk(tmp_path: Path) -> None:
    """A re-scan removes documents whose files vanished — the dir-delete self-heal.

    Directory-removal fires no per-file event, so the periodic reconcile relies
    on this: CollectionSyncJob's disk-vs-registry delta deletes documents no
    longer on disk. Here a file is indexed, then removed, then re-scanned.
    """
    db, settings, root = _fixture(tmp_path)
    (root / "keep.md").write_text("still here")
    (root / "gone.md").write_text("about to vanish")
    with patch(
        "quarry.ingestion.streaming.get_embedding_backend", return_value=_FakeEmbedder()
    ):
        asyncio.run(
            CollectionSyncJob(db, settings, "col", root).run(
                _dummy_ctx(), TaskState(task_id="scan1", kind="sync")
            )
        )
        assert _docs(db) == {"keep.md", "gone.md"}

        (root / "gone.md").unlink()  # simulate the file/dir removal
        state = TaskState(task_id="scan2", kind="sync")
        rescan = CollectionSyncJob(db, settings, "col", root)
        asyncio.run(rescan.run(_dummy_ctx(), state))

    assert state.status == "completed"
    assert state.results["deleted"] == 1
    assert _docs(db) == {"keep.md"}  # the gone document was purged from the index


def test_deregister_purge_after_queued_index_leaves_no_orphans(tmp_path: Path) -> None:
    """A deregister purge cleans up even a racing queued index job (DES-045 blocker).

    A FileIndexJob for the collection is admitted, then deregister removes the
    registry rows and routes a collection-wide purge onto the SAME FIFO worker.
    The insert runs first (and orphans its chunks — its registry upsert fails on
    the now-gone directory), then the purge deletes the whole collection, so no
    orphan chunk survives.  Routing the purge through the queue (not a direct
    out-of-queue delete) is what orders it after the insert.
    """
    base = tmp_path / "data" / "testdb"
    (base / "lancedb").mkdir(parents=True)
    root = tmp_path / "proj"
    root.mkdir()
    settings = Settings(
        lancedb_path=base / "lancedb", registry_path=base / "registry.db"
    )
    conn = SyncRegistry(settings.registry_path)
    try:
        conn.register_directory(root.resolve(), "col")
    finally:
        conn.close()
    (root / "x.md").write_text("indexable body text that will orphan then purge")

    async def _run() -> None:
        ctx = DaemonContext(settings, embedder=_FakeEmbedder())
        key = RouteKey(ctx.database_name, "col")
        # 1. an index job for the collection is admitted (in-flight).
        index_state = ctx.tasks.begin("index")
        job = FileIndexJob(
            ctx.database, ctx.settings, "col", root.resolve(), root.resolve() / "x.md"
        )
        assert ctx.ingest_queue.try_submit(key, job, index_state)
        # 2. deregister drops the registry rows, then purges THROUGH the queue —
        #    FIFO behind the index job, so it runs after the insert.
        dconn = SyncRegistry(settings.registry_path)
        try:
            dconn.deregister_directory("col")
        finally:
            dconn.close()
        purge_state = ctx.tasks.begin("deregister")
        purge_state.results = {"deleted_chunks": 0}
        await RegistrationLifecycle(ctx).run_purge(purge_state, "col")
        await ctx.aclose_ingest_queue()
        # 3. no orphan chunks survive for the deregistered collection.
        assert _docs(ctx.database) == set()

    with patch(
        "quarry.ingestion.streaming.get_embedding_backend", return_value=_FakeEmbedder()
    ):
        asyncio.run(_run())


def test_registering_a_parent_purges_subsumed_child_chunks(tmp_path: Path) -> None:
    """Registering a parent over a child tears it down and purges its chunks.

    A child collection has an in-flight FileIndexJob whose chunks orphan once the
    parent registration deletes the child's directories row.  _run_register must
    route a collection-wide purge onto the child's FIFO — behind the insert — so
    no orphan chunk survives in the subsumed collection.
    """
    base = tmp_path / "data" / "testdb"
    (base / "lancedb").mkdir(parents=True)
    parent = tmp_path / "proj"
    child = parent / "sub"
    child.mkdir(parents=True)
    settings = Settings(
        lancedb_path=base / "lancedb", registry_path=base / "registry.db"
    )
    conn = SyncRegistry(settings.registry_path)
    try:
        conn.register_directory(child.resolve(), "child-col")
    finally:
        conn.close()
    (child / "x.md").write_text("indexable body text that will orphan then purge")

    async def _run() -> None:
        ctx = DaemonContext(settings, embedder=_FakeEmbedder())
        key = RouteKey(ctx.database_name, "child-col")
        # 1. an index job for the child collection is admitted (in-flight).
        index_state = ctx.tasks.begin("index")
        job = FileIndexJob(
            ctx.database,
            ctx.settings,
            "child-col",
            child.resolve(),
            child.resolve() / "x.md",
        )
        assert ctx.ingest_queue.try_submit(key, job, index_state)
        # 2. registering the parent subsumes child-col and purges it THROUGH the
        #    queue — FIFO behind the index job, so the purge runs after the insert.
        reg_state = ctx.tasks.begin("register")
        await RegistrationLifecycle(ctx).run_register(
            reg_state, parent.resolve(), "parent-col"
        )
        assert reg_state.status == "completed"
        assert reg_state.results["subsumed"] == ["child-col"]
        await ctx.aclose_ingest_queue()
        # 3. no orphan chunks survive in the subsumed child collection.
        assert _docs(ctx.database) == set()

    with patch(
        "quarry.ingestion.streaming.get_embedding_backend", return_value=_FakeEmbedder()
    ):
        asyncio.run(_run())


def test_subsume_purge_failure_is_logged_and_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A subsumed child whose purge cannot be admitted is surfaced, never swallowed.

    With the queue permanently full and a zero admission deadline, the subsume
    purge fails.  Register itself still completes, but the failed child must be
    logged and recorded in the task result — an unpurged child leaves orphan
    chunks with no reconcile backstop, so silence would hide unrecoverable data.
    """
    base = tmp_path / "data" / "testdb"
    (base / "lancedb").mkdir(parents=True)
    parent = tmp_path / "proj"
    child = parent / "sub"
    child.mkdir(parents=True)
    settings = Settings(
        lancedb_path=base / "lancedb", registry_path=base / "registry.db"
    )
    conn = SyncRegistry(settings.registry_path)
    try:
        conn.register_directory(child.resolve(), "child-col")
    finally:
        conn.close()

    monkeypatch.setattr("quarry.daemon.purge_service._PURGE_SUBMIT_DEADLINE_S", 0.0)

    async def _run() -> None:
        ctx = DaemonContext(settings, embedder=_FakeEmbedder())
        # A permanently-full queue: the purge can never be admitted.  Patch the
        # class (IngestQueue is __slots__ed, so the instance attr is read-only).
        monkeypatch.setattr(IngestQueue, "try_submit", lambda *_a, **_k: False)
        reg_state = ctx.tasks.begin("register")
        with caplog.at_level(logging.WARNING):
            await RegistrationLifecycle(ctx).run_register(
                reg_state, parent.resolve(), "parent-col"
            )
        # Register succeeds; the failed child is both logged and reported.
        assert reg_state.status == "completed"
        assert reg_state.results["subsume_purge_failed"] == ["child-col"]
        assert "subsume purge failed for collection child-col" in caplog.text

    asyncio.run(_run())
