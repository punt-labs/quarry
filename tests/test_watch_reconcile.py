"""Tests for WatchReconciler's durable orphan sweep (the A data-safety cases).

The sweep deletes chunks of any collection that is neither registered nor
retained.  The non-negotiable invariant (I6): a registered or operator-kept
(keep-data) collection is NEVER swept.  These tests exercise a real LanceDB +
registry so the DB-derived orphan computation is genuine, not mocked.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast, final
from unittest.mock import patch

import numpy as np

from quarry.config import Settings
from quarry.daemon.context import DaemonContext
from quarry.daemon.index_jobs import FileIndexJob
from quarry.daemon.route_key import RouteKey
from quarry.daemon.watch_reconcile import ReconcilerDeps, WatchReconciler
from quarry.db.chunk_catalog import ChunkCatalog
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from numpy.typing import NDArray

    from quarry.daemon.watch_roster import WatchRoster
    from quarry.daemon.watch_submit import WatchSubmitter
    from quarry.results import CollectionSummary

_DIM = 768
_TERMINAL = frozenset({"completed", "failed"})


@final
class _FakeEmbedder:
    """A hermetic embedder: random vectors, no ONNX."""

    __slots__ = ()

    @property
    def dimension(self) -> int:
        return _DIM

    @property
    def model_name(self) -> str:
        return "reconcile-fake"

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        rng = np.random.default_rng(0)
        return rng.standard_normal((len(texts), _DIM)).astype(np.float32)

    def embed_query(self, query: str) -> NDArray[np.float32]:
        return np.zeros(_DIM, dtype=np.float32)


def _sweep_only_deps(ctx: DaemonContext) -> ReconcilerDeps:
    """Deps whose roster/submitter are unused — the sweep touches only ``ctx``."""
    return ReconcilerDeps(
        ctx=ctx,
        roster=cast("WatchRoster", object()),
        submitter=cast("WatchSubmitter", object()),
        begin=lambda *_a: None,
        teardown=lambda *_a: None,
    )


def _fresh_settings(tmp_path: Path) -> Settings:
    """Return Settings backed by a fresh LanceDB + registry under *tmp_path*."""
    base = tmp_path / "data" / "testdb"
    (base / "lancedb").mkdir(parents=True)
    return Settings(lancedb_path=base / "lancedb", registry_path=base / "registry.db")


def _register_with_bodies(settings: Settings, dirs: dict[str, Path]) -> None:
    """Register each ``name -> directory`` and drop one indexable file in it."""
    conn = SyncRegistry(settings.registry_path)
    try:
        for name, directory in dirs.items():
            directory.mkdir()
            conn.register_directory(directory.resolve(), name)
            (directory / "x.md").write_text(f"indexable body for {name}")
    finally:
        conn.close()


async def _index_and_wait(ctx: DaemonContext, roots: dict[str, Path]) -> None:
    """Index each collection into LanceDB and wait until every job is terminal."""
    states = [
        state
        for name, root in roots.items()
        if (state := ctx.tasks.begin("index"))
        and ctx.ingest_queue.try_submit(
            RouteKey(ctx.database_name, name),
            FileIndexJob(ctx.database, ctx.settings, name, root, root / "x.md"),
            state,
        )
    ]
    deadline = asyncio.get_running_loop().time() + 30.0
    while any(s.status not in _TERMINAL for s in states):
        assert asyncio.get_running_loop().time() < deadline, "index jobs stuck"
        await asyncio.sleep(0.05)


def _live_collections(ctx: DaemonContext) -> set[str]:
    """Return the collection names currently holding chunks in LanceDB."""
    return {c["collection"] for c in ctx.database.catalog.list_collections()}


def test_sweep_never_touches_registered_or_retained_collections(tmp_path: Path) -> None:
    """Orphan chunks are swept; registered + retained collections are spared (I6).

    Three collections gain chunks: "reg" stays registered, "keep" is
    keep-data-deregistered (retained), "orphan" is deregistered with no marker.
    The sweep must purge ONLY "orphan" — deleting a registered or operator-kept
    collection's chunks would be data loss.
    """
    settings = _fresh_settings(tmp_path)
    roots = {name: (tmp_path / name).resolve() for name in ("reg", "keep", "orphan")}
    _register_with_bodies(settings, roots)

    async def _run() -> None:
        ctx = DaemonContext(settings, embedder=_FakeEmbedder())
        await _index_and_wait(ctx, roots)
        assert _live_collections(ctx) == {"reg", "keep", "orphan"}

        # Transition the registry: "keep" retained (chunks kept), "orphan" dropped.
        conn = SyncRegistry(settings.registry_path)
        try:
            conn.deregister_directory("keep", keep_data=True)
            conn.deregister_directory("orphan")  # no marker → a genuine orphan
        finally:
            conn.close()

        # "reg" stays registered, "keep" is retained, "orphan" is neither. The
        # sweep derives registered + retained from the registry itself.
        await WatchReconciler(_sweep_only_deps(ctx))._sweep_orphans()
        await ctx.aclose_ingest_queue()

        # orphan swept; registered + kept spared
        assert _live_collections(ctx) == {"reg", "keep"}

    with patch(
        "quarry.ingestion.streaming.get_embedding_backend",
        return_value=_FakeEmbedder(),
    ):
        asyncio.run(_run())


def test_reregister_during_sweep_spares_retained_collection(tmp_path: Path) -> None:
    """A collection re-registered mid-sweep is NOT purged (djb item 3, data loss).

    ``keep`` is chunk-bearing and keep-data-deregistered (retained).  During the
    off-thread chunk scan, ``keep`` is re-registered — which atomically commits
    its directory row AND clears its retained marker.  Because the sweep reads
    ``registered`` and ``retained`` from ONE registry connection AFTER the scan,
    it sees ``keep`` back in ``directories`` and spares it.  Deriving
    ``registered`` from a pre-scan roster snapshot would misclassify ``keep`` as
    an orphan and wipe the operator's kept chunks.
    """
    settings = _fresh_settings(tmp_path)
    roots = {name: (tmp_path / name).resolve() for name in ("keep", "orphan")}
    _register_with_bodies(settings, roots)

    async def _run() -> None:
        ctx = DaemonContext(settings, embedder=_FakeEmbedder())
        await _index_and_wait(ctx, roots)

        conn = SyncRegistry(settings.registry_path)
        try:
            conn.deregister_directory("keep", keep_data=True)
            conn.deregister_directory("orphan")
        finally:
            conn.close()

        # Re-register "keep" as a side effect of the sweep's chunk scan, i.e. in
        # the window the stale-snapshot bug read across. The single-connection
        # read that follows must observe it. Patched at the class — the slotted
        # catalog instance has no settable attribute.
        real_scan = ChunkCatalog.list_collections

        def _reregister_keep_then_scan(cat: ChunkCatalog) -> list[CollectionSummary]:
            live = SyncRegistry(settings.registry_path)
            try:
                live.register_directory(roots["keep"], "keep")
            finally:
                live.close()
            return real_scan(cat)

        with patch.object(ChunkCatalog, "list_collections", _reregister_keep_then_scan):
            await WatchReconciler(_sweep_only_deps(ctx))._sweep_orphans()
        await ctx.aclose_ingest_queue()

        # "keep" re-registered mid-sweep → spared; "orphan" still swept.
        assert _live_collections(ctx) == {"keep"}

    with patch(
        "quarry.ingestion.streaming.get_embedding_backend",
        return_value=_FakeEmbedder(),
    ):
        asyncio.run(_run())


def test_sweep_fail_closed_on_scan_error(tmp_path: Path) -> None:
    """A raising chunk scan skips the cycle, never kills the loop (djb item 4).

    The sweep's reads run unguarded before this fix, so a transient LanceDB or
    SQLite error propagated out of ``run_safety_loop`` (which catches only
    ``CancelledError``) and permanently stopped all reconciles.  Now a failed
    read fail-closes: the sweep logs and returns, the orphan survives this cycle,
    and the very next sweep — with the scan healthy — purges it.
    """
    settings = _fresh_settings(tmp_path)
    roots = {"orphan": (tmp_path / "orphan").resolve()}
    _register_with_bodies(settings, roots)

    async def _run() -> None:
        ctx = DaemonContext(settings, embedder=_FakeEmbedder())
        await _index_and_wait(ctx, roots)

        conn = SyncRegistry(settings.registry_path)
        try:
            conn.deregister_directory("orphan")
        finally:
            conn.close()

        reconciler = WatchReconciler(_sweep_only_deps(ctx))

        def _raise_scan(_catalog: ChunkCatalog) -> list[CollectionSummary]:
            raise OSError("transient scan failure")

        with patch.object(ChunkCatalog, "list_collections", _raise_scan):
            await reconciler._sweep_orphans()  # must NOT raise
        assert _live_collections(ctx) == {"orphan"}  # skipped this cycle

        await reconciler._sweep_orphans()  # scan healthy again → self-heals
        await ctx.aclose_ingest_queue()
        assert _live_collections(ctx) == set()  # orphan purged next cycle

    with patch(
        "quarry.ingestion.streaming.get_embedding_backend",
        return_value=_FakeEmbedder(),
    ):
        asyncio.run(_run())
