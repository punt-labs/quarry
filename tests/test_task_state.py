"""TaskState ends only through complete() or fail(); no field is settable."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from quarry.daemon.task_state import TaskState

if TYPE_CHECKING:
    from quarry.daemon.task_state import TaskStatus


@pytest.mark.parametrize(
    "field",
    ["status", "error", "results", "holds_thread", "store", "claim", "task_id", "kind"],
)
def test_no_field_is_publicly_settable(field: str) -> None:
    state = TaskState("t", "sync")

    with pytest.raises(AttributeError):
        setattr(state, field, "x")


def test_a_new_task_is_running_with_nothing_recorded() -> None:
    state = TaskState("t", "sync")

    assert (state.status, state.error, state.results) == ("running", "", {})


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_a_task_cannot_begin_in_a_terminal_status(status: TaskStatus) -> None:
    with pytest.raises(ValueError, match="complete"):
        TaskState("t", "sync", status=status)


def test_complete_records_the_results_and_clears_the_reason() -> None:
    state = TaskState("t", "sync")
    state.note("an earlier reason")

    state.complete({"count": 3})

    assert (state.status, state.error, state.results) == ("completed", "", {"count": 3})


def test_fail_records_the_reason_and_keeps_what_was_recorded() -> None:
    state = TaskState("t", "sync")
    state.record({"count": 1})

    state.fail("disk gone")

    assert (state.status, state.error, state.results) == (
        "failed",
        "disk gone",
        {"count": 1},
    )


def test_fail_refuses_an_empty_reason_and_leaves_the_task_running() -> None:
    state = TaskState("t", "sync")

    with pytest.raises(ValueError, match="needs a reason"):
        state.fail("")

    assert state.status == "running"


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_move_to_refuses_a_terminal_status(status: TaskStatus) -> None:
    state = TaskState("t", "sync")

    with pytest.raises(ValueError, match="complete"):
        state.move_to(status)

    assert state.status == "running"


def test_results_is_a_copy_the_caller_cannot_use_to_edit_the_task() -> None:
    state = TaskState("t", "sync")
    state.complete({"count": 3})

    state.results["count"] = 99

    assert state.results == {"count": 3}


def test_the_thread_guard_is_held_then_released() -> None:
    state = TaskState("t", "sync")

    state.hold_thread()
    assert state.holds_thread
    state.release_thread()

    assert not state.holds_thread
