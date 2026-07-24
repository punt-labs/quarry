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
    from collections.abc import Callable
    from pathlib import Path

    from numpy.typing import NDArray

    from quarry.daemon.watch_roster import WatchRoster
    from quarry.daemon.watch_submit import WatchSubmitter
    from quarry.results import CollectionSummary
    from quarry.sync_registry import DirectoryRegistration

_DIM = 768
_TERMINAL = frozenset({"completed", "failed"})


@final
class _AlienScanError(Exception):
    """A scan failure subclassing none of the stdlib recoverable types.

    Stands in for a LanceDB/pyarrow error outside ``(OSError, ValueError,
    sqlite3.Error, RuntimeError, TimeoutError)`` — the backstop must still
    fail-closed on it, not just on the enumerated stdlib set.
    """


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


def _between_reads_mutation(
    settings: Settings, mutate: Callable[[SyncRegistry], object]
) -> Callable[[SyncRegistry], list[DirectoryRegistration]]:
    """Patch for ``list_registrations`` that commits *mutate* between the reads.

    ``_read_orphans`` reads ``list_registrations`` then ``list_retained``. This
    wrapper runs the real first read, then commits *mutate* on a separate
    connection — landing the commit in the window BETWEEN the two SELECTs, the
    exact interleaving the read-transaction fix must make invisible. A one-shot
    guard fires the mutation only for the outer read (``register``/``deregister``
    themselves call ``list_registrations`` and must not re-enter it).
    """
    real = SyncRegistry.list_registrations
    fired: list[bool] = []

    def _wrapper(reg: SyncRegistry) -> list[DirectoryRegistration]:
        result = real(reg)
        if not fired:
            fired.append(True)
            writer = SyncRegistry(settings.registry_path)
            try:
                mutate(writer)
            finally:
                writer.close()
        return result

    return _wrapper


def test_reregister_between_sweep_reads_spares_collection(tmp_path: Path) -> None:
    """A re-register committing BETWEEN the two sweep reads spares the collection.

    ``keep`` is chunk-bearing and keep-data-deregistered (retained).  The
    re-register (retained→directories) commits AFTER ``list_registrations`` is
    read but BEFORE ``list_retained`` — so without one read snapshot, ``keep`` is
    absent from ``registered`` (read pre-commit, still only retained) AND absent
    from ``retained`` (read post-commit, marker cleared): it falls through both
    sets and its kept chunks are purged.  The ``BEGIN`` read transaction makes
    the mid-read commit invisible to both SELECTs, so ``keep`` is seen retained
    and spared.  This FAILS without the transaction and passes with it.
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

        patched = _between_reads_mutation(
            settings, lambda w: w.register_directory(roots["keep"], "keep")
        )
        with patch.object(SyncRegistry, "list_registrations", patched):
            await WatchReconciler(_sweep_only_deps(ctx))._sweep_orphans()
        await ctx.aclose_ingest_queue()

        # "keep" re-registered mid-read → spared; "orphan" still swept.
        assert _live_collections(ctx) == {"keep"}

    with patch(
        "quarry.ingestion.streaming.get_embedding_backend",
        return_value=_FakeEmbedder(),
    ):
        asyncio.run(_run())


def test_keepdata_deregister_between_sweep_reads_spares_collection(
    tmp_path: Path,
) -> None:
    """The symmetric direction: a keep-data deregister BETWEEN the reads is safe.

    ``keep`` starts registered and chunk-bearing.  A keep-data deregister
    (directories→retained) commits between ``list_registrations`` and
    ``list_retained``.  Under a single read snapshot ``keep`` is seen registered
    (its pre-commit state) and spared.  This is the direction a future reordering
    of the two reads would expose to the same fall-through, so the transaction —
    not the read order — is what guarantees safety.  ``orphan`` is a genuine
    orphan and must still be swept, proving the sweep ran.
    """
    settings = _fresh_settings(tmp_path)
    roots = {name: (tmp_path / name).resolve() for name in ("keep", "orphan")}
    _register_with_bodies(settings, roots)

    async def _run() -> None:
        ctx = DaemonContext(settings, embedder=_FakeEmbedder())
        await _index_and_wait(ctx, roots)

        conn = SyncRegistry(settings.registry_path)
        try:
            conn.deregister_directory("orphan")  # a genuine orphan
        finally:
            conn.close()

        patched = _between_reads_mutation(
            settings, lambda w: w.deregister_directory("keep", keep_data=True)
        )
        with patch.object(SyncRegistry, "list_registrations", patched):
            await WatchReconciler(_sweep_only_deps(ctx))._sweep_orphans()
        await ctx.aclose_ingest_queue()

        # "keep" kept (retained mid-read) → spared; "orphan" swept.
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
    and the very next sweep — with the scan healthy — purges it.  The raised
    error subclasses none of the stdlib recoverable types, proving the guard
    catches ANY read failure (incl. LanceDB/pyarrow errors), not just those.
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
            raise _AlienScanError("transient scan failure")

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
