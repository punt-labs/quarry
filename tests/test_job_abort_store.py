"""A store that outlives its worker: the job it settles and the record it removes.

Each test drives ``IngestQueue.aclose`` or ``CollectionWorker.abort_off_loop`` with
real jobs, a real ``JobLifecycle`` and a real ``JobSpool`` in ``tmp_path``.  The
store of a job is a real function blocked on an event the test sets; a spool write
blocks through ``JobSpool.write``.  No test assigns ``state.status``.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.store_run import StoreRun
from quarry.daemon.task_terminal import TaskTerminal
from quarry.daemon.tasks import TaskState
from quarry.ingestion.file_indexer import FileIndexOutcome
from quarry.refusal import RefusalError
from tests.job_abort_world import WRITES, World, terms, until

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.job_abort_world import Entry


def status_of(entry: Entry) -> str:
    """Return a job's status as of now, as a plain string."""
    return str(entry.state.status)


SETTLED = {"completed", "failed", "withdrawn", "aborted"}


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    with World(tmp_path) as w:
        WRITES.clear()
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


# T2 -- I2: the terms still sum to the jobs taken when a store lands late.
async def test_t2_terms_sum_to_the_jobs_taken_while_stores_land_late(
    world: World,
) -> None:
    flight = world.admit("infl", collection="c1")
    await world.running("infl")
    world.admit("sq", collection="c1")
    commit = world.admit("cm", collection="c2", backfill=True, commits=True)
    await world.running("cm")
    gate = world.hold_spool("infl")

    await world.abort()
    assert _terms_add_up(world)["unconfirmed"] == 1

    commit.store.land()
    await world.landed(commit)
    assert _terms_add_up(world)["kept"] == 1
    assert terms(world.summary())["unconfirmed"] == 0

    flight.store.land()
    await world.landed(flight)
    gate.release.set()
    await until(lambda: terms(world.summary())["unfinished"] == 0)
    final = _terms_add_up(world)
    assert (final["kept"], final["jobs"]) == (2, 3)
    await until(lambda: len(world.spool_files()) == 1)  # only the queued job's


# T1 -- I1: every taken job ends in a status that matches the table.
@pytest.mark.parametrize("stored", [True, False])
async def test_t1_every_taken_job_reaches_a_status_that_matches_its_store(
    world: World, *, stored: bool
) -> None:
    flight = world.admit("flight", collection="c1")
    commit = world.admit("commit", collection="c2", backfill=True, commits=True)
    await world.running("flight", "commit")

    await world.abort()
    outcome = None if stored else RefusalError("nothing to store")
    flight.store.land(outcome)
    commit.store.land(outcome)
    await world.landed(flight, commit)

    want = "completed" if stored else "failed"
    assert status_of(flight) == want
    assert status_of(commit) == want
    assert bool(flight.state.results) is stored
    assert bool(commit.state.results) is stored


# T4 -- I4: a store that lands after the cancel is not reported lost.
async def test_t4_a_store_that_lands_after_the_abort_reports_completed(
    world: World,
) -> None:
    entry = world.admit("late")
    await world.running("late")

    await world.abort()
    assert status_of(entry) == "failed"
    entry.store.land()
    await world.landed(entry)

    assert status_of(entry) == "completed"
    assert entry.state.results
    assert terms(world.summary())["kept"] == 1


async def test_t4_a_store_that_ended_before_the_cancel_arrived_is_completed(
    world: World,
) -> None:
    entry = world.admit("race")
    await world.running("race")
    entry.store.land()
    assert entry.store.ended.wait(5)

    # Nothing yields between the thread's end and the cancel: the loop has not
    # yet delivered the store's answer to the worker.
    task = world.worker("c1").abort_off_loop()
    await asyncio.wait({task})
    await world.landed(entry)

    assert status_of(entry) == "completed"
    assert entry.state.results
    assert terms(world.summary())["kept"] == 1


# T7 -- I7: a status the producer or the store settled is never overwritten.
@pytest.mark.parametrize("lands", [True, False])
async def test_t7_kept_statuses_stay_and_a_committing_job_follows_its_store(
    world: World, *, lands: bool
) -> None:
    running = world.admit("run", collection="c1")
    await world.running("run")
    waiting = world.admit("wait", collection="c1")
    commit = world.admit("cm", collection="c2", backfill=True, commits=True)
    await world.running("cm")
    JobLifecycle(running.state).withdraw()
    JobLifecycle(waiting.state).withdraw()

    await world.abort()

    assert status_of(running) == "aborted"
    assert status_of(waiting) == "withdrawn"
    assert status_of(commit) == "committing"

    commit.store.land(None if lands else RefusalError("insert failed"))
    await world.landed(commit)
    assert status_of(commit) == ("completed" if lands else "failed")
    if not lands:
        assert "re-run backfill" in commit.state.error
    assert status_of(running) == "aborted"


# T9 -- liveness: once every event is released, every job is settled.
async def test_t9_every_job_settles_and_nothing_is_pending(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    entries = [
        world.admit("a", collection="c1"),
        world.admit("b", collection="c1"),
        world.admit("c", collection="c2", backfill=True, commits=True),
    ]
    await world.running("a", "c")
    JobLifecycle(entries[1].state).withdraw()
    gate = world.hold_spool("a")

    with caplog.at_level(logging.WARNING, logger="quarry.daemon.queue_abort"):
        await world.abort()
    gate.release.set()
    for entry in entries:
        entry.store.land()
    await until(lambda: world.pending() == 0)
    await world.landed(entries[0], entries[2])

    assert all(e.state.status in SETTLED for e in entries)
    assert [e.state.status for e in entries] == ["completed", "withdrawn", "completed"]
    assert world.pending() == 0


# T10 -- I11: a job that stored leaves no spool file behind.
async def test_t10_a_store_after_the_spool_removes_the_record(world: World) -> None:
    entry = world.admit("ghost")
    await world.running("ghost")

    await world.abort()
    assert len(world.spool_files()) == 1
    entry.store.land()
    await world.landed(entry)

    assert status_of(entry) == "completed"
    await until(lambda: world.spool_files() == [])
    assert terms(world.summary())["spooled"] == 0
    assert terms(world.summary())["kept"] == 1


async def test_t10_a_store_while_the_spool_runs_leaves_no_record(
    world: World,
) -> None:
    entry = world.admit("ghost2")
    await world.running("ghost2")
    gate = world.hold_spool("ghost2")

    await world.abort()
    entry.store.land()
    await world.landed(entry)
    gate.release.set()
    await asyncio.sleep(0.2)
    await until(lambda: world.spool_files() == [])

    assert status_of(entry) == "completed"
    assert terms(world.summary())["kept"] == 1


# T11 -- A1: stores never starve the spool.
async def test_t11_a_spool_write_runs_while_every_store_thread_is_blocked(
    world: World,
) -> None:
    for n in range(8):
        world.admit(f"s{n}", collection=f"k{n}")
    await world.running(*(f"s{n}" for n in range(8)))
    world.admit("later", collection="k0")

    await world.abort()

    assert len(world.spool_files()) >= 1


def test_t11_the_store_executor_is_not_the_shared_pool() -> None:
    assert StoreRun.threads() is not DaemonThreads.shared()


# T13 -- I9, I10: a late store clears the reason; a failed commit leaves no stale term.
async def test_t13_a_late_store_clears_the_reason_and_sets_the_results(
    world: World,
) -> None:
    entry = world.admit("late")
    await world.running("late")

    await world.abort()
    assert entry.state.error
    entry.store.land()
    await world.landed(entry)

    assert status_of(entry) == "completed"
    assert entry.state.error == ""
    assert entry.state.results


async def test_t13_a_failed_committing_store_is_failed_and_needs_no_spool(
    world: World,
) -> None:
    commit = world.admit("cm", backfill=True, commits=True)
    await world.running("cm")

    await world.abort()
    assert terms(world.summary())["unconfirmed"] == 1
    commit.store.land(RefusalError("insert failed"))
    await world.landed(commit)

    assert status_of(commit) == "failed"
    assert terms(world.summary())["unconfirmed"] == 0
    assert terms(world.summary())["no_spool"] == 1


# T14 -- the observer's hand-back never raises into the store thread.
def test_t14_a_store_that_ends_after_the_loop_closed_raises_nothing(
    tmp_path: Path,
) -> None:
    errors: list[object] = []
    hook = threading.excepthook
    threading.excepthook = errors.append
    try:
        with World(tmp_path) as world:
            entry = {}

            async def scenario() -> None:
                entry["e"] = world.admit("closed")
                await world.running("closed")
                await world.abort()

            asyncio.run(scenario())
            entry["e"].store.land()
            assert entry["e"].store.ended.wait(5)
            threading.Event().wait(0.2)
    finally:
        threading.excepthook = hook
    assert errors == []


# T15 -- the observer's contract.
async def test_t15_a_detached_store_that_raises_is_retrieved_logged_once_and_redacted(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    unretrieved: list[str] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: unretrieved.append(str(context.get("message")))
    )
    entry = world.admit("boom")
    await world.running("boom")

    with caplog.at_level(logging.DEBUG):
        await world.abort()
        entry.store.land(RuntimeError("fetch https://secret.example/?token=abc failed"))
        await until(lambda: entry.store.ended.is_set())
        await asyncio.sleep(0.2)
        gc.collect()
        await asyncio.sleep(0.05)

    assert unretrieved == []
    failures = [r for r in caplog.records if "store failed" in r.getMessage()]
    assert len(failures) == 1
    assert "secret.example" not in caplog.text
    assert "token=abc" not in caplog.text
    assert "secret.example" not in entry.state.error


async def test_t15_zero_rows_from_a_detached_store_is_never_completed(
    world: World,
) -> None:
    commit = world.admit("zero", backfill=True, commits=True)
    await world.running("zero")

    await world.abort()
    commit.store.land({"chunks": 0})
    await world.landed(commit)

    assert status_of(commit) == "failed"


async def test_t15_an_index_job_error_is_its_own(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = world.admit_index_jobs(monkeypatch)
    index = entries[0]
    index.store.outcome = FileIndexOutcome("doc", 0, "the file vanished")
    await until(index.store.started.is_set)

    index.store.land()
    await world.landed(index)

    assert status_of(index) == "failed"
    assert index.state.error == "the file vanished"


def _cancel() -> None:
    raise asyncio.CancelledError


def _break() -> None:
    msg = "disk gone"
    raise RuntimeError(msg)


@pytest.mark.parametrize("kind", ["delete", "sync", "umbrella", "register"])
async def test_t15_callers_outside_the_queue_end_as_before(kind: str) -> None:
    done = TaskState(task_id="a", kind=kind)
    with TaskTerminal.recording(done):
        done.status = "completed"
    failed = TaskState(task_id="b", kind=kind)
    with TaskTerminal.recording(failed):
        _break()
    cancelled = TaskState(task_id="c", kind=kind)
    with pytest.raises(asyncio.CancelledError), TaskTerminal.recording(cancelled):
        _cancel()

    assert done.status == "completed"
    assert (failed.status, failed.error) == ("failed", "internal error: RuntimeError")
    assert (cancelled.status, cancelled.error) == ("failed", "task was cancelled")


def test_t16_the_process_exits_with_a_store_that_never_returns(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parent.parent
    script = tmp_path / "stuck.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import asyncio, sys, threading
            sys.path.insert(0, {str(root)!r})
            from tests.job_abort_world import World, until

            async def main():
                with World(__import__("pathlib").Path({str(tmp_path)!r})) as w:
                    w.admit("stuck", backfill=True, commits=True)
                    await w.running("stuck")
                    await w.abort()
                    print("aborted", flush=True)
                    import os
                    os._exit(0) if False else None
            asyncio.run(main())
            print("exiting", flush=True)
            """
        )
    )
    # The patched store waits on an event the script never sets (30 s at most):
    # the interpreter must still exit at once because its threads are daemons.
    result = subprocess.run(
        [sys.executable, "-I", str(script)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert "exiting" in result.stdout, result.stderr
