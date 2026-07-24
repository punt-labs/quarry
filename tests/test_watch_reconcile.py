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
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from numpy.typing import NDArray

    from quarry.daemon.watch_roster import WatchRoster
    from quarry.daemon.watch_submit import WatchSubmitter

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


def test_sweep_never_touches_registered_or_retained_collections(tmp_path: Path) -> None:
    """Orphan chunks are swept; registered + retained collections are spared (I6).

    Three collections gain chunks: "reg" stays registered, "keep" is
    keep-data-deregistered (retained), "orphan" is deregistered with no marker.
    The sweep must purge ONLY "orphan" — deleting a registered or operator-kept
    collection's chunks would be data loss.
    """
    base = tmp_path / "data" / "testdb"
    (base / "lancedb").mkdir(parents=True)
    settings = Settings(
        lancedb_path=base / "lancedb", registry_path=base / "registry.db"
    )
    roots: dict[str, Path] = {}
    conn = SyncRegistry(settings.registry_path)
    try:
        for name in ("reg", "keep", "orphan"):
            directory = tmp_path / name
            directory.mkdir()
            conn.register_directory(directory.resolve(), name)
            (directory / "x.md").write_text(f"indexable body for {name}")
            roots[name] = directory.resolve()
    finally:
        conn.close()

    async def _run() -> None:
        ctx = DaemonContext(settings, embedder=_FakeEmbedder())
        # Index all three collections (chunks land in LanceDB under each name).
        states = []
        for name, root in roots.items():
            state = ctx.tasks.begin("index")
            job = FileIndexJob(ctx.database, ctx.settings, name, root, root / "x.md")
            key = RouteKey(ctx.database_name, name)
            assert ctx.ingest_queue.try_submit(key, job, state)
            states.append(state)
        deadline = asyncio.get_running_loop().time() + 30.0
        while any(s.status not in _TERMINAL for s in states):
            assert asyncio.get_running_loop().time() < deadline, "index jobs stuck"
            await asyncio.sleep(0.05)
        assert {c["collection"] for c in ctx.database.catalog.list_collections()} == {
            "reg",
            "keep",
            "orphan",
        }

        # Transition the registry: "keep" retained (chunks kept), "orphan" dropped.
        conn2 = SyncRegistry(settings.registry_path)
        try:
            conn2.deregister_directory("keep", keep_data=True)
            conn2.deregister_directory("orphan")  # no marker → a genuine orphan
        finally:
            conn2.close()

        # Sweep with "reg" live; "keep" is retained, "orphan" is neither.
        live = {RouteKey(ctx.database_name, "reg")}
        await WatchReconciler(_sweep_only_deps(ctx))._sweep_orphans(live)
        await ctx.aclose_ingest_queue()

        surviving = {c["collection"] for c in ctx.database.catalog.list_collections()}
        assert surviving == {"reg", "keep"}  # orphan swept; registered + kept spared

    with patch(
        "quarry.ingestion.streaming.get_embedding_backend",
        return_value=_FakeEmbedder(),
    ):
        asyncio.run(_run())
