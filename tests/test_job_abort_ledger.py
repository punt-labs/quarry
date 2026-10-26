"""The abort's ledger: every taken job is in one term, and a late outcome moves it once.

Each test drives ``IngestQueue.aclose`` or ``CollectionWorker.abort_off_loop`` with
real jobs, a real ``JobLifecycle`` and a real ``JobSpool`` in ``tmp_path``.  The
store of a job is a real function blocked on an event the test sets; a spool write
blocks through ``JobSpool.write``.  No test assigns ``state.status``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.job_lifecycle import JobLifecycle
from tests.job_abort_world import World, terms, until

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    with World(tmp_path) as w:
        yield w


def _terms_add_up(w: World) -> dict[str, int]:
    found = terms(w.summary())
    parts = [
        "spooled",
        "unrecoverable",
        "lost",
        "unfinished",
        "no_spool",
        "kept",
        "unconfirmed",
    ]
    assert sum(found[p] for p in parts) == found["jobs"], w.summary()
    return found


# T2 -- I2: the terms always sum to the jobs taken, and kept jobs are kept.
async def test_t2_terms_sum_to_the_jobs_taken_at_every_read(world: World) -> None:
    world.admit("infl", collection="c1")
    await world.running("infl")
    withdrawn = world.admit("wd", collection="c1")
    world.admit("sq", collection="c1")
    world.admit("cm", collection="c2", backfill=True, commits=True)
    await world.running("cm")
    gate = world.hold_spool("infl")
    JobLifecycle(withdrawn.state).withdraw()

    await world.abort()
    after_deadline = _terms_add_up(world)
    assert after_deadline["jobs"] == 4
    assert after_deadline["kept"] == 1
    assert after_deadline["unconfirmed"] == 1
    assert after_deadline["unfinished"] == 1

    gate.release.set()
    await until(lambda: terms(world.summary())["unfinished"] == 0)
    after_late = _terms_add_up(world)
    assert after_late["spooled"] == 2
    assert after_late["unconfirmed"] == 1


# T3 -- I3: a job the producer withdrew is never spooled and is counted kept.
async def test_t3_a_withdrawn_or_aborted_job_is_counted_kept_and_not_spooled(
    world: World,
) -> None:
    running = world.admit("run", collection="c1")
    await world.running("run")
    queued = world.admit("que", collection="c1")

    task = world.worker("c1").abort_off_loop()
    JobLifecycle(queued.state).withdraw()
    JobLifecycle(running.state).withdraw()
    await asyncio.wait({task})

    assert queued.state.status == "withdrawn"
    assert running.state.status == "aborted"
    assert world.spool_files() == []
    assert terms(world.summary())["kept"] == 2
    assert world.pending() == 0


# T5 -- I5: a late correction moves the reason and the term together, once.
async def test_t5_a_late_spool_corrects_the_reason_and_the_term_together(
    world: World,
) -> None:
    entry = world.admit("slow")
    await world.running("slow")
    gate = world.hold_spool("slow")

    await world.abort()
    assert terms(world.summary())["unfinished"] == 1
    assert "unfinished" in entry.state.error

    gate.release.set()
    await until(lambda: terms(world.summary())["spooled"] == 1)
    assert "spooled for recovery" in entry.state.error
    assert terms(world.summary())["unfinished"] == 0
    assert len(world.spool_files()) == 1
    await asyncio.sleep(0.05)
    assert terms(world.summary())["spooled"] == 1


# T8 -- I8: a reason says only what reached the disk.
async def test_t8_a_cancelled_spool_future_is_unfinished_not_a_failed_write(
    world: World,
) -> None:
    entry = world.admit("cancelled")
    await world.running("cancelled")
    gate = world.hold_spool("cancelled")
    shared = DaemonThreads.shared()

    async def cancel_spool_futures() -> None:
        await until(gate.started.is_set)
        for task in asyncio.all_tasks():
            frame = getattr(task.get_coro(), "cr_frame", None)
            if frame is not None and frame.f_locals.get("self") is shared:
                task.cancel()

    canceller = asyncio.ensure_future(cancel_spool_futures())
    await world.abort()
    await canceller

    assert "spool write failed" not in entry.state.error
    assert terms(world.summary())["lost"] == 0
    assert terms(world.summary())["unfinished"] == 1
    gate.release.set()
    await until(lambda: len(world.spool_files()) == 1)
    assert "spool write failed" not in entry.state.error


async def test_t8_a_failed_write_says_so_and_leaves_no_record(
    world: World, tmp_path: Path
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    world.break_spool(blocker)
    entry = world.admit("bad")
    await world.running("bad")

    await world.abort()

    assert world.spool_files() == []
    assert "spool write failed" in entry.state.error
    assert terms(world.summary())["lost"] == 1


# T12 -- A4: a cancelled abort records its queued job as unfinished.
async def test_t12_a_cancelled_queued_spool_is_unfinished_not_pending(
    world: World,
) -> None:
    world.admit("run", collection="c1")
    await world.running("run")
    queued = world.admit("que", collection="c1")
    gate = world.hold_spool("que")

    task = world.worker("c1").abort_off_loop()
    await until(gate.started.is_set)
    task.cancel()
    await asyncio.wait({task})

    await until(lambda: world.pending() == 0)  # the in-flight job's spool ends
    assert terms(world.summary())["unfinished"] == 1
    gate.release.set()
    await until(lambda: terms(world.summary())["unfinished"] == 0)
    assert "spooled for recovery" in queued.state.error
    assert terms(world.summary())["spooled"] == 2


# T16 -- a committing job whose store never ends stays unconfirmed; the process exits.
async def test_t16_a_committing_job_with_a_stuck_store_is_unconfirmed(
    world: World,
) -> None:
    commit = world.admit("stuck", backfill=True, commits=True)
    await world.running("stuck")

    await world.abort()

    assert commit.state.status == "committing"
    assert terms(world.summary())["unconfirmed"] == 1
