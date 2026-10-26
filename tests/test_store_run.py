"""StoreRun: a job's store on its own threads, read by the worker or the observer."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, final

import pytest

from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.job_spool import SpoolRecord
from quarry.daemon.settlement import Settlement
from quarry.daemon.store_run import StoreRun
from quarry.refusal import RefusalError
from tests.job_states import job_in

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.tasks import TaskState


@final
@dataclass(frozen=True, slots=True)
class _Job:
    """The job a store belongs to: only its collection is read."""

    collection: str = "c"

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        del ctx, state

    def spool_record(self) -> SpoolRecord | None:
        return None


def _job() -> TaskState:
    return job_in("running")


def _start(state: TaskState, call: Callable[[], dict[str, object]]) -> StoreRun:
    return StoreRun.start(_Job(), state, call, _stored)


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

    run = _start(_job(), call)

    assert (await run.wait()).results == {"chunks": 1}
    assert seen == [(True, "quarry-store")]


async def test_the_run_is_kept_on_the_state_for_the_abort_to_find() -> None:
    state = _job()

    run = _start(state, dict)
    await run.wait()

    assert state.store is run


async def test_a_store_that_raised_raises_in_its_waiter() -> None:
    def refuses() -> dict[str, object]:
        msg = "refused"
        raise RefusalError(msg)

    run = _start(_job(), refuses)

    with pytest.raises(RefusalError, match="refused"):
        await run.wait()


async def test_a_cancelled_waiter_leaves_the_store_running() -> None:
    release = threading.Event()
    run = _start(_job(), _blocked(release))
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

    run = _start(_job(), breaks)
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
    run = _start(state, _blocked(release))
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

    run = _start(state, breaks)
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


async def test_a_store_is_described_by_its_kind_job_collection_and_task() -> None:
    state = _job()
    release = threading.Event()
    run = _start(state, _blocked(release))

    assert run.describe() == f"remember _Job for c (task {state.task_id})"
    release.set()
    await run.wait()


async def test_a_detached_store_is_unobserved_until_its_end_is_read() -> None:
    release = threading.Event()
    run = _start(_job(), _blocked(release))
    with_its_worker = run.is_unobserved
    run.detach()
    detached = run.is_unobserved
    release.set()
    await asyncio.wait_for(run.observed(), 5)

    assert (with_its_worker, detached, run.is_unobserved) == (False, True, False)


async def test_a_detached_store_that_was_refused_is_one_warning_without_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _job()

    def refuses() -> dict[str, object]:
        msg = "nothing to store"
        raise RefusalError(msg)

    run = _start(state, refuses)
    run.detach()
    with caplog.at_level(logging.WARNING, logger="quarry.daemon"):
        await asyncio.wait_for(run.observed(), 5)

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "store refused: nothing to store" in record.getMessage()


async def test_a_store_the_loop_cancelled_is_named_and_leaves_the_job_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _job()
    release = threading.Event()
    before = asyncio.all_tasks()
    run = _start(state, _blocked(release))
    [store] = asyncio.all_tasks() - before
    run.detach()
    with caplog.at_level(logging.WARNING, logger="quarry.daemon"):
        store.cancel()  # the loop's close cancels what is still pending
        await asyncio.wait_for(run.observed(), 5)

    assert "store still running at shutdown" in caplog.text
    assert state.status == "running"
    release.set()
