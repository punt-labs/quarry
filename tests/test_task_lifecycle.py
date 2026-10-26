"""TaskLifecycle: a polled task is running until its body says otherwise."""

from __future__ import annotations

import pytest

from quarry.daemon.task_lifecycle import TaskLifecycle
from quarry.daemon.tasks import TaskState, TaskStatus


def _task(status: TaskStatus = "running") -> TaskState:
    return TaskState(task_id="t", kind="sync", status=status)


def test_a_task_completes_with_its_results_and_no_reason() -> None:
    state = _task()
    state.error = "an earlier reason"

    moved = TaskLifecycle(state).settle_stored({"count": 3})
    assert moved

    assert (state.status, state.error, state.results) == ("completed", "", {"count": 3})


def test_a_task_fails_with_its_reason() -> None:
    state = _task()

    moved = TaskLifecycle(state).settle_failed("disk gone")
    assert moved

    assert (state.status, state.error) == ("failed", "disk gone")


@pytest.mark.parametrize("recorded", ["running", "completed", "failed"])
def test_a_cancel_fails_the_task_whatever_it_had_recorded(recorded: TaskStatus) -> None:
    state = _task(recorded)

    TaskLifecycle(state).cancelled("task was cancelled")

    assert (state.status, state.error) == ("failed", "task was cancelled")


def test_a_cancel_keeps_the_reason_the_task_had_given() -> None:
    state = _task()
    state.error = "backfill stopped; scan thread still running"

    TaskLifecycle(state).cancelled("task was cancelled")

    assert state.error == "backfill stopped; scan thread still running"


@pytest.mark.parametrize("status", ["running", "queued", "committing", "aborted"])
def test_a_body_that_exits_unfinished_fails(status: TaskStatus) -> None:
    state = _task(status)

    TaskLifecycle(state).exited("task exited without setting terminal status")

    assert (state.status, state.error) == (
        "failed",
        "task exited without setting terminal status",
    )


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_a_body_that_exits_finished_is_left_alone(status: TaskStatus) -> None:
    state = _task(status)

    TaskLifecycle(state).exited("task exited without setting terminal status")

    assert state.status == status
    assert state.error == ""
