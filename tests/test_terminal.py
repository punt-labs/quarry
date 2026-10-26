"""Terminal: a task error fails closed, and a queued job never over a settled status."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

import pytest

from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.tasks import TaskState
from quarry.daemon.terminal import Terminal
from tests.job_states import JobStates

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    with caplog.at_level(logging.ERROR), Terminal.for_task(state).recording():
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
    with pytest.raises(asyncio.CancelledError), Terminal.for_task(state).recording():
        _fail(asyncio.CancelledError())
    assert (state.status, state.error) == ("failed", "task was cancelled")


def test_a_body_that_never_set_a_status_is_marked_failed() -> None:
    state = _state()
    with Terminal.for_task(state).recording():
        pass
    assert (state.status, state.error) == (
        "failed",
        "task exited without setting terminal status",
    )


def test_a_cancel_never_overwrites_the_reason_the_run_already_gave() -> None:
    state = _state()
    state.error = "backfill stopped (daemon shutting down); scan thread still running"

    with pytest.raises(asyncio.CancelledError), Terminal.for_task(state).recording():
        _fail(asyncio.CancelledError())

    assert state.status == "failed"
    assert state.error == (
        "backfill stopped (daemon shutting down); scan thread still running"
    )


@pytest.mark.parametrize("status", ["queued", "running", "committing", "aborted"])
def test_a_body_that_exits_mid_job_is_forced_to_failed(status: TaskStatus) -> None:
    state = TaskState(task_id="t-2", kind="backfill", status=status)

    with Terminal.for_task(state).recording():
        pass

    assert (state.status, state.error) == (
        "failed",
        "task exited without setting terminal status",
    )


def test_a_body_that_exits_mid_job_keeps_the_reason_it_gave() -> None:
    state = TaskState(task_id="t-3", kind="backfill", status="committing")
    state.error = "insert failed"

    with Terminal.for_task(state).recording():
        pass

    assert (state.status, state.error) == ("failed", "insert failed")


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_a_terminal_status_is_left_alone(status: TaskStatus) -> None:
    state = TaskState(task_id="t-4", kind="backfill", status=status)

    with Terminal.for_task(state).recording():
        pass

    assert state.status == status


_LEAKY_MESSAGE = "boom https://u:USEC@h.test/p?token=QSEC"


def test_an_exception_fails_the_job_with_only_its_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = JobStates.reaching("running")
    with caplog.at_level(logging.ERROR), Terminal.for_job(state).recording():
        _fail(RuntimeError(_LEAKY_MESSAGE))

    assert (state.status, state.error) == ("failed", "internal error: RuntimeError")
    assert "QSEC" not in caplog.text


def test_a_cancel_is_recorded_and_reraised() -> None:
    state = JobStates.reaching("running")
    with pytest.raises(asyncio.CancelledError), Terminal.for_job(state).recording():
        _fail(asyncio.CancelledError())

    assert (state.status, state.error) == ("failed", "task was cancelled")


def test_a_body_that_settled_the_job_is_left_alone() -> None:
    state = JobStates.reaching("running")
    with Terminal.for_job(state).recording():
        moved = JobLifecycle(state).settle_stored({"chunks": 1})
        assert moved

    assert state.status == "completed"


def test_a_body_that_settled_nothing_fails_the_job() -> None:
    state = JobStates.reaching("running")
    with Terminal.for_job(state).recording():
        pass

    assert (state.status, state.error) == (
        "failed",
        "task exited without setting terminal status",
    )


@pytest.mark.parametrize("status", ["withdrawn", "aborted"])
def test_a_status_the_producer_settled_is_never_overwritten(
    status: TaskStatus,
) -> None:
    state = JobStates.reaching(status)
    with Terminal.for_job(state).recording():
        _fail(RuntimeError("late"))

    assert state.status == status


def test_a_cancel_of_a_committing_job_leaves_it_to_its_store() -> None:
    state = JobStates.reaching("committing")
    with pytest.raises(asyncio.CancelledError), Terminal.for_job(state).recording():
        _fail(asyncio.CancelledError())

    assert state.status == "committing"


@final
class _Spy:
    """A lifecycle that only records the moves a terminal makes on it."""

    __slots__ = ("_moves",)

    _moves: list[str]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._moves = []
        return self

    @property
    def moves(self) -> list[str]:
        return list(self._moves)

    def settle_stored(self, results: Mapping[str, object]) -> bool:
        self._moves.append(f"stored {dict(results)}")
        return True

    def settle_failed(self, reason: str) -> bool:
        self._moves.append(f"failed {reason}")
        return True

    def cancelled(self, default: str) -> None:
        self._moves.append(f"cancelled {default}")

    def exited(self, default: str) -> None:
        self._moves.append(f"exited {default}")


def test_a_terminal_drives_whatever_lifecycle_it_is_given() -> None:
    spy = _Spy()
    terminal = Terminal(_state(), spy)

    with pytest.raises(asyncio.CancelledError), terminal.recording():
        _fail(asyncio.CancelledError())

    assert spy.moves == [
        "cancelled task was cancelled",
        "exited task exited without setting terminal status",
    ]


def test_a_terminal_hands_the_settle_moves_to_its_lifecycle() -> None:
    spy = _Spy()
    terminal = Terminal(_state(), spy)

    stored = terminal.settle_stored({"chunks": 1})
    failed = terminal.settle_failed("refused")

    assert (stored, failed) == (True, True)
    assert spy.moves == ["stored {'chunks': 1}", "failed refused"]
    assert (terminal.kind, terminal.task_id) == ("sync", "t-1")
