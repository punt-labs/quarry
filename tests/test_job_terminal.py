"""JobTerminal: a queued job's body ends it, never over a status someone settled."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pytest

from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.job_terminal import JobTerminal
from tests.job_states import job_in

if TYPE_CHECKING:
    from quarry.daemon.tasks import TaskStatus


_LEAKY_MESSAGE = "boom https://u:USEC@h.test/p?token=QSEC"


def _raise(error: BaseException) -> None:
    raise error


def test_an_exception_fails_the_job_with_only_its_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = job_in("running")
    with caplog.at_level(logging.ERROR), JobTerminal.recording(state):
        _raise(RuntimeError(_LEAKY_MESSAGE))

    assert (state.status, state.error) == ("failed", "internal error: RuntimeError")
    assert "QSEC" not in caplog.text


def test_a_cancel_is_recorded_and_reraised() -> None:
    state = job_in("running")
    with pytest.raises(asyncio.CancelledError), JobTerminal.recording(state):
        _raise(asyncio.CancelledError())

    assert (state.status, state.error) == ("failed", "task was cancelled")


def test_a_body_that_settled_the_job_is_left_alone() -> None:
    state = job_in("running")
    with JobTerminal.recording(state):
        assert JobLifecycle(state).settle_stored({"chunks": 1})

    assert state.status == "completed"


def test_a_body_that_settled_nothing_fails_the_job() -> None:
    state = job_in("running")
    with JobTerminal.recording(state):
        pass

    assert (state.status, state.error) == (
        "failed",
        "task exited without setting terminal status",
    )


@pytest.mark.parametrize("status", ["withdrawn", "aborted"])
def test_a_status_the_producer_settled_is_never_overwritten(
    status: TaskStatus,
) -> None:
    state = job_in(status)
    with JobTerminal.recording(state):
        _raise(RuntimeError("late"))

    assert state.status == status


def test_a_cancel_of_a_committing_job_leaves_it_to_its_store() -> None:
    state = job_in("committing")
    with pytest.raises(asyncio.CancelledError), JobTerminal.recording(state):
        _raise(asyncio.CancelledError())

    assert state.status == "committing"
