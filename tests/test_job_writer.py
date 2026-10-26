"""A queued job's status has one writer: every write holds the claim, in the lifecycle.

Each test drives ``IngestQueue.aclose`` with real jobs, a real ``JobLifecycle`` and
a real ``JobSpool``; the store of a job is a real function blocked on an event the
test sets.  A producer thread withdraws every job in a loop while the abort runs.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from quarry.daemon.job_lifecycle import JobLifecycle
from tests.job_abort_world import SETTLED, WRITES, World, until

if TYPE_CHECKING:
    import pytest


# every write of a status, error or results holds the claim, in the lifecycle.
async def test_every_status_write_holds_the_claim_and_is_the_lifecycles(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = world.admit_index_jobs(monkeypatch)
    entries = [
        world.admit("flight", collection="c1"),
        world.admit("commit", collection="c2", backfill=True, commits=True),
        *sources,
    ]
    await world.running("flight", "commit")
    await until(lambda: all(s.store.started.is_set() for s in sources))
    stop = threading.Event()

    def producer() -> None:
        while not stop.is_set():
            for entry in entries:
                JobLifecycle(entry.state).withdraw()

    thread = threading.Thread(target=producer)
    thread.start()
    try:
        await world.abort()
        for entry in entries:
            entry.store.land()
        await until(
            lambda: all(e.state.status in SETTLED | {"committing"} for e in entries)
        )
    finally:
        stop.set()
        thread.join()

    unowned = [w for w in WRITES if not w[2]]
    outside = {w[3] for w in WRITES} - {"job_lifecycle.py"}
    assert unowned == []
    assert outside == set()
