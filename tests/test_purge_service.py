"""Tests for CollectionPurger: purge routing and admission-failure reporting.

The through-queue happy path (a purge that deletes a collection's chunks behind
its inserts) is exercised end-to-end in ``test_index_jobs``.  This file isolates
the purger's own failure contract: a queue that never admits the purge must
yield a failed task with a reason, never a hang or a crash (bug class 2).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from quarry.config import Settings
from quarry.daemon.context import DaemonContext
from quarry.daemon.ingest_queue import IngestQueue
from quarry.daemon.purge_service import CollectionPurger

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_purge_reports_failure_when_queue_never_admits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A purge the saturated queue never admits returns a failed task with a reason."""
    base = tmp_path / "data" / "db"
    (base / "lancedb").mkdir(parents=True)
    settings = Settings(lancedb_path=base / "lancedb", registry_path=base / "r.db")
    # Zero deadline + a permanently-full queue: admission fails on the first try.
    monkeypatch.setattr("quarry.daemon.purge_service._PURGE_SUBMIT_DEADLINE_S", 0.0)
    monkeypatch.setattr(IngestQueue, "try_submit", lambda *_a, **_k: False)

    async def _run() -> None:
        ctx = DaemonContext(settings)
        purge = await CollectionPurger(ctx).purge("col", "test-purge")
        assert purge.status == "failed"
        assert purge.error and "queue full" in purge.error

    asyncio.run(_run())
