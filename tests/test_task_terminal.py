"""Tests for quarry.daemon.task_terminal.TaskTerminal: a task error fails closed."""

from __future__ import annotations

import asyncio
import logging

import pytest

from quarry.daemon.task_terminal import TaskTerminal
from quarry.daemon.tasks import TaskState


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
    assert record.exc_info[1] is secret


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
