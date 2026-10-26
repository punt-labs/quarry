"""StoreRun: a job's store on its own threads, read by the worker or the observer."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

import pytest

from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.settlement import Settlement
from quarry.daemon.store_run import StoreRun
from quarry.daemon.tasks import TaskState
from quarry.refusal import RefusalError

if TYPE_CHECKING:
    from collections.abc import Callable


def _job() -> TaskState:
    state = TaskState(task_id="j", kind="remember", status="new")
    assert JobLifecycle(state).enqueue()
    assert JobLifecycle(state).start()
    return state


def _stored(results: dict[str, object]) -> Settlement:
    return Settlement.done(results)


def _blocked(release: threading.Event) -> Callable[[], dict[str, object]]:
    def call() -> dict[str, object]:
        release.wait(5)
        return {"chunks": 2}

    return call


def test_the_store_pool_is_its_own_and_one_for_the_daemon() -> None:
    assert StoreRun.threads() is StoreRun.threads()
    assert StoreRun.threads() is not DaemonThreads.shared()


async def test_a_store_runs_on_a_daemon_thread_of_the_store_pool() -> None:
    seen: list[tuple[bool, str]] = []

    def call() -> dict[str, object]:
        thread = threading.current_thread()
        seen.append((thread.daemon, thread.name))
        return {"chunks": 1}

    run = StoreRun.start(_job(), call, _stored)

    assert (await run.wait()).results == {"chunks": 1}
    assert seen == [(True, "quarry-store")]


async def test_the_run_is_kept_on_the_state_for_the_abort_to_find() -> None:
    state = _job()

    run = StoreRun.start(state, dict, _stored)
    await run.wait()

    assert state.store is run


async def test_a_store_that_raised_raises_in_its_waiter() -> None:
    def refuses() -> dict[str, object]:
        msg = "refused"
        raise RefusalError(msg)

    run = StoreRun.start(_job(), refuses, _stored)

    with pytest.raises(RefusalError, match="refused"):
        await run.wait()


async def test_a_cancelled_waiter_leaves_the_store_running() -> None:
    release = threading.Event()
    run = StoreRun.start(_job(), _blocked(release), _stored)
    waiting = asyncio.ensure_future(run.wait())
    await asyncio.sleep(0.02)

    waiting.cancel()
    await asyncio.wait({waiting})

    assert run.outcome() is None
    release.set()
    for _ in range(100):
        if run.outcome() is not None:
            break
        await asyncio.sleep(0.01)
    outcome = run.outcome()
    assert outcome is not None
    assert outcome.results == {"chunks": 2}


async def test_the_outcome_of_a_store_that_raised_is_a_failure_in_the_jobs_words() -> (
    None
):
    def breaks() -> dict[str, object]:
        msg = "https://secret.example/?token=abc"
        raise RuntimeError(msg)

    run = StoreRun.start(_job(), breaks, _stored)
    with pytest.raises(RuntimeError):
        await run.wait()

    outcome = run.outcome()
    assert outcome is not None
    assert not outcome.is_stored
    assert outcome.reason == "internal error: RuntimeError"


async def test_a_detached_store_that_lands_completes_its_job_and_tells_listeners() -> (
    None
):
    state = _job()
    release = threading.Event()
    run = StoreRun.start(state, _blocked(release), _stored)
    told: list[JobLifecycle.Observed] = []
    run.listen(told.append)
    JobLifecycle(state).cancelled("task was cancelled")
    assert state.status == "failed"

    release.set()
    for _ in range(100):
        if told:
            break
        await asyncio.sleep(0.01)

    assert told == [JobLifecycle.Observed.COMPLETED]
    assert (state.status, state.error, state.results) == (
        "completed",
        "",
        {"chunks": 2},
    )


async def test_a_detached_store_that_raised_is_logged_once_and_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _job()
    release = threading.Event()

    def breaks() -> dict[str, object]:
        release.wait(5)
        msg = "https://secret.example/?token=abc"
        raise RuntimeError(msg)

    run = StoreRun.start(state, breaks, _stored)
    told: list[JobLifecycle.Observed] = []
    run.listen(told.append)
    with caplog.at_level(logging.ERROR, logger="quarry.daemon"):
        JobLifecycle(state).cancelled("task was cancelled")
        release.set()
        for _ in range(100):
            if told:
                break
            await asyncio.sleep(0.01)

    assert told == [JobLifecycle.Observed.UNCHANGED]
    assert len([r for r in caplog.records if "store failed" in r.getMessage()]) == 1
    assert "secret.example" not in caplog.text
    assert state.status == "failed"
