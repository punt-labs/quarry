"""JobLifecycle: a job runs, commits or is withdrawn; a withdrawn one never writes."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.tasks import TaskState

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskStatus

_CTX = cast("DaemonContext", SimpleNamespace())
_Withdrawn = JobLifecycle.Withdrawn


def _state(status: TaskStatus) -> TaskState:
    return TaskState(task_id="j", kind="backfill", status=status)


class _Job:
    def __init__(self) -> None:
        self.ran = 0

    async def run(self, ctx: object, state: TaskState) -> None:
        del ctx
        self.ran += 1
        state.status = "completed"


def _queued() -> TaskState:
    state = _state("new")
    assert JobLifecycle(state).enqueue()
    return state


def test_a_queued_job_starts_once() -> None:
    state = _state("queued")

    assert JobLifecycle(state).start()
    assert state.status == "running"
    assert not JobLifecycle(state).start()


def test_a_job_withdrawn_before_it_was_queued_stays_withdrawn() -> None:
    state = _state("new")
    JobLifecycle(state).withdraw()

    assert not JobLifecycle(state).enqueue()
    assert state.status == "withdrawn"


@pytest.mark.parametrize("status", ["running", "committing", "completed", "aborted"])
def test_starting_a_job_in_any_other_status_is_logged_at_warning(
    status: TaskStatus, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="quarry.daemon.job_lifecycle"):
        assert not JobLifecycle(_state(status)).start()

    assert status in caplog.text


def test_starting_a_withdrawn_job_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    state = _state("queued")
    JobLifecycle(state).withdraw()

    with caplog.at_level(logging.WARNING, logger="quarry.daemon.job_lifecycle"):
        assert not JobLifecycle(state).start()

    assert caplog.text == ""


def test_a_job_withdrawn_before_it_runs_never_starts_and_says_so() -> None:
    state = _state("queued")

    assert JobLifecycle(state).withdraw() is _Withdrawn.NEVER_STARTED
    assert JobLifecycle(state).withdraw() is _Withdrawn.NEVER_STARTED  # still
    assert not JobLifecycle(state).start()


def test_a_job_that_was_never_queued_is_withdrawn_too() -> None:
    assert JobLifecycle(_state("new")).withdraw() is _Withdrawn.NEVER_STARTED


def test_a_job_withdrawn_while_running_stops_at_its_commit() -> None:
    state = _state("queued")
    assert JobLifecycle(state).start()

    assert JobLifecycle(state).withdraw() is _Withdrawn.STARTED
    assert not JobLifecycle(state).commit()  # it writes nothing
    assert JobLifecycle(state).withdraw() is _Withdrawn.STARTED  # and still says it ran


def test_a_job_past_its_commit_cannot_be_withdrawn() -> None:
    state = _state("queued")
    assert JobLifecycle(state).start()

    assert JobLifecycle(state).commit()
    assert JobLifecycle(state).withdraw() is _Withdrawn.COMMITTING
    assert state.status == "committing"


@pytest.mark.parametrize(
    ("status", "verdict"),
    [("completed", _Withdrawn.STORED), ("failed", _Withdrawn.FAILED)],
)
def test_a_finished_job_is_reported_as_it_finished(
    status: TaskStatus, verdict: JobLifecycle.Withdrawn
) -> None:
    state = _state(status)

    assert JobLifecycle(state).withdraw() is verdict
    assert state.status == status


async def test_a_queued_job_runs_under_the_gate() -> None:
    state, job = _queued(), _Job()

    await JobLifecycle(state).run(cast("IngestUnit", job), _CTX, asyncio.Semaphore(1))

    assert (job.ran, state.status) == (1, "completed")


async def test_a_job_withdrawn_before_the_gate_is_skipped_without_taking_it() -> None:
    state, job = _queued(), _Job()
    JobLifecycle(state).withdraw()
    gate = asyncio.Semaphore(0)  # a held gate: taking it would hang

    await asyncio.wait_for(
        JobLifecycle(state).run(cast("IngestUnit", job), _CTX, gate), timeout=2
    )

    assert job.ran == 0


async def test_a_job_withdrawn_while_it_waits_for_the_gate_is_skipped_at_the_gate() -> (
    None
):
    state, job = _queued(), _Job()
    gate = asyncio.Semaphore(0)
    running = asyncio.create_task(
        JobLifecycle(state).run(cast("IngestUnit", job), _CTX, gate)
    )
    await asyncio.sleep(0.05)  # it is queued, waiting on the gate

    JobLifecycle(state).withdraw()
    gate.release()
    await asyncio.wait_for(running, timeout=2)

    assert job.ran == 0
    assert state.status == "withdrawn"


async def test_a_job_that_is_not_queued_is_not_run_twice() -> None:
    state, job = _queued(), _Job()
    assert JobLifecycle(state).start()  # already dispatched once

    await JobLifecycle(state).run(cast("IngestUnit", job), _CTX, asyncio.Semaphore(1))

    assert job.ran == 0


async def test_a_job_that_is_not_queued_warns_when_it_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state, job = _queued(), _Job()
    assert JobLifecycle(state).start()

    with caplog.at_level(logging.WARNING, logger="quarry.daemon.job_lifecycle"):
        await JobLifecycle(state).run(
            cast("IngestUnit", job), _CTX, asyncio.Semaphore(1)
        )

    assert "running" in caplog.text


def _job(status: TaskStatus) -> TaskState:
    state = _queued()
    state.status = status
    return state


@pytest.mark.parametrize("status", ["running", "committing"])
def test_settle_stored_completes_a_job_the_worker_owns_and_clears_its_reason(
    status: TaskStatus,
) -> None:
    state = _job(status)
    state.error = "daemon shut down before ingest"

    assert JobLifecycle(state).settle_stored({"chunks": 2})

    assert (state.status, state.error, state.results) == (
        "completed",
        "",
        {"chunks": 2},
    )


@pytest.mark.parametrize("status", ["withdrawn", "aborted", "completed", "failed"])
def test_settle_never_overwrites_a_status_somebody_else_settled(
    status: TaskStatus,
) -> None:
    state = _job(status)
    state.error = "kept"

    assert not JobLifecycle(state).settle_stored({"chunks": 2})
    assert not JobLifecycle(state).settle_failed("late")

    assert (state.status, state.error, state.results) == (status, "kept", {})


@pytest.mark.parametrize("status", ["running", "committing"])
def test_settle_failed_fails_a_job_the_worker_owns(status: TaskStatus) -> None:
    state = _job(status)

    assert JobLifecycle(state).settle_failed("refused")

    assert (state.status, state.error) == ("failed", "refused")


def test_a_cancel_fails_a_running_job_but_leaves_the_others() -> None:
    kept = {s: _job(s) for s in ("committing", "aborted", "completed", "withdrawn")}
    running = _job("running")

    for state in (running, *kept.values()):
        JobLifecycle(state).cancelled("task was cancelled")

    assert (running.status, running.error) == ("failed", "task was cancelled")
    assert {s: state.status for s, state in kept.items()} == {s: s for s in kept}


def test_a_task_that_never_was_a_job_is_failed_by_a_cancel_whatever_it_recorded() -> (
    None
):
    state = _state("completed")

    JobLifecycle(state).cancelled("task was cancelled")

    assert (state.status, state.error) == ("failed", "task was cancelled")


def test_an_exit_with_no_terminal_status_fails_only_a_running_job() -> None:
    running, aborted = _job("running"), _job("aborted")

    for state in (running, aborted):
        JobLifecycle(state).exited("task exited without setting terminal status")

    assert running.status == "failed"
    assert aborted.status == "aborted"


@pytest.mark.parametrize(
    ("status", "disposition"),
    [
        ("withdrawn", JobLifecycle.Disposition.KEPT),
        ("aborted", JobLifecycle.Disposition.KEPT),
        ("completed", JobLifecycle.Disposition.KEPT),
        ("committing", JobLifecycle.Disposition.UNCONFIRMED),
        ("queued", JobLifecycle.Disposition.FAILED),
        ("running", JobLifecycle.Disposition.FAILED),
        ("failed", JobLifecycle.Disposition.FAILED),
    ],
)
def test_the_abort_fails_only_what_it_owns(
    status: TaskStatus, disposition: JobLifecycle.Disposition
) -> None:
    state = _job(status)

    assert JobLifecycle(state).fail_aborted("shut down") is disposition

    failed = disposition is JobLifecycle.Disposition.FAILED
    assert (state.status == "failed") is failed
    assert (state.error == "shut down") is failed
