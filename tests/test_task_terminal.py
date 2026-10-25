"""Tests for quarry.daemon.task_terminal.TaskTerminal: a task error fails closed."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pytest

from quarry.daemon.task_terminal import TaskTerminal
from quarry.daemon.tasks import TaskState

if TYPE_CHECKING:
    from quarry.daemon.tasks import TaskStatus


def _state() -> TaskState:
    return TaskState(task_id="t-1", kind="sync")


def _fail(error: BaseException) -> None:
    raise error


def test_an_arbitrary_exception_records_only_its_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _state()
    secret = RuntimeError("boom https://u:USEC@h.test/p?token=QSEC /home/me/.ssh/key")
    with caplog.at_level(logging.ERROR), TaskTerminal.recording(state):
        _fail(secret)

    assert (state.status, state.error) == ("failed", "internal error: RuntimeError")
    [record] = caplog.records
    assert record.getMessage() == "Background sync failed"
    assert record.exc_info is not None
    assert record.exc_info[1] is not secret  # the stand-in names the class only
    assert str(record.exc_info[1]) == "RuntimeError"
    assert "QSEC" not in caplog.text


def test_cancellation_is_recorded_and_reraised() -> None:
    state = _state()
    with pytest.raises(asyncio.CancelledError), TaskTerminal.recording(state):
        _fail(asyncio.CancelledError())
    assert (state.status, state.error) == ("failed", "task was cancelled")


def test_a_body_that_never_set_a_status_is_marked_failed() -> None:
    state = _state()
    with TaskTerminal.recording(state):
        pass
    assert (state.status, state.error) == (
        "failed",
        "task exited without setting terminal status",
    )


def test_a_cancel_never_overwrites_the_reason_the_run_already_gave() -> None:
    state = _state()
    state.error = "backfill stopped (daemon shutting down); scan thread still running"

    with pytest.raises(asyncio.CancelledError), TaskTerminal.recording(state):
        _fail(asyncio.CancelledError())

    assert state.status == "failed"
    assert state.error == (
        "backfill stopped (daemon shutting down); scan thread still running"
    )


@pytest.mark.parametrize("status", ["queued", "running", "committing", "aborted"])
def test_a_body_that_exits_mid_job_is_forced_to_failed(status: TaskStatus) -> None:
    state = TaskState(task_id="t-2", kind="backfill", status=status)

    with TaskTerminal.recording(state):
        pass

    assert (state.status, state.error) == (
        "failed",
        "task exited without setting terminal status",
    )


def test_a_body_that_exits_mid_job_keeps_the_reason_it_gave() -> None:
    state = TaskState(task_id="t-3", kind="backfill", status="committing")
    state.error = "insert failed"

    with TaskTerminal.recording(state):
        pass

    assert (state.status, state.error) == ("failed", "insert failed")


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_a_terminal_status_is_left_alone(status: TaskStatus) -> None:
    state = TaskState(task_id="t-4", kind="backfill", status=status)

    with TaskTerminal.recording(state):
        pass

    assert state.status == status
