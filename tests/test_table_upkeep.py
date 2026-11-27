"""TableUpkeep's slow-clock sweep: a failed sweep is retried by the next finalize."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from quarry.db.orphan_pages import OrphanPageSweep
from quarry.db.table_changes import TableChanges
from quarry.post_sync_gc import PostSyncGc
from quarry.table_upkeep import TableUpkeep

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    import pytest


def test_a_failed_rewrite_sweep_is_retried_however_recent_the_last_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[float] = []
    now = [0.0]
    failing = [False]

    def sweep(_sweep: OrphanPageSweep, _now: datetime | None = None) -> int:
        attempts.append(now[0])
        if failing[0]:
            msg = "disk"
            raise OSError(msg)
        return 0

    monkeypatch.setattr(OrphanPageSweep, "run", sweep)
    monkeypatch.setattr(PostSyncGc, "run", lambda _gc: None)
    settings = MagicMock(name="settings")
    settings.lancedb_path = tmp_path / "db" / "lancedb"
    settings.quarry_root = tmp_path
    changes = TableChanges(clock=lambda: now[0])
    upkeep = TableUpkeep(MagicMock(name="db"), settings, changes)

    upkeep.skip_rewrite()  # t0: due, and it succeeds
    assert attempts == [0.0]
    now[0] = 600.0
    failing[0] = True
    upkeep.settle_rewrite(upkeep.snapshot(), succeeded=True)  # t0+10 min: it fails
    assert attempts == [0.0, 600.0]
    now[0] = 660.0
    failing[0] = False
    upkeep.skip_rewrite()  # t0+11 min: a clean finalize retries the sweep
    assert attempts == [0.0, 600.0, 660.0]
    now[0] = 720.0
    upkeep.skip_rewrite()  # the success restarted the clock
    assert attempts == [0.0, 600.0, 660.0]
