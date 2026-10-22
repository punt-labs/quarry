"""Tests for quarry.daemon.refusal_aware_terminal: a refusal ends a task quietly."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from quarry.content_guard import UnindexableContentError
from quarry.daemon.refusal_aware_terminal import RefusalAwareTerminal
from quarry.daemon.tasks import TaskState
from quarry.db.row_budget import RowBudgetError

if TYPE_CHECKING:
    import pytest


def _raise(error: Exception) -> None:
    raise error


def _state() -> TaskState:
    return TaskState(task_id="t-1", kind="remember")


def test_a_refusal_fails_the_task_with_one_warning_and_no_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _state()
    with caplog.at_level(logging.WARNING), RefusalAwareTerminal(state).record():
        _raise(UnindexableContentError("binary content rejected: pdf header"))

    assert (state.status, state.error) == (
        "failed",
        "binary content rejected: pdf header",
    )
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None


def test_any_other_error_is_still_logged_with_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _state()
    with caplog.at_level(logging.WARNING), RefusalAwareTerminal(state).record():
        _raise(RuntimeError("boom"))

    assert (state.status, state.error) == ("failed", "boom")
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None


def test_a_body_that_sets_its_own_status_is_left_alone() -> None:
    state = _state()
    with RefusalAwareTerminal(state).record():
        state.status = "completed"
    assert state.status == "completed"


def test_a_body_that_sets_no_status_is_marked_failed() -> None:
    state = _state()
    with RefusalAwareTerminal(state).record():
        pass
    assert state.status == "failed"


def test_the_warning_names_the_task_and_the_subject(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = TaskState(task_id="remember-abc", kind="remember")
    with (
        caplog.at_level(logging.WARNING),
        RefusalAwareTerminal(state, lambda: "note.md").record(),
    ):
        _raise(UnindexableContentError("binary content rejected: pdf header"))

    [record] = caplog.records
    assert record.getMessage() == (
        "Background remember note.md (task remember-abc) rejected: "
        "binary content rejected: pdf header"
    )


def test_the_subject_is_built_only_when_a_refusal_is_logged() -> None:
    calls: list[int] = []

    def subject() -> str:
        calls.append(1)
        return "x"

    state = _state()
    with RefusalAwareTerminal(state, subject).record():
        state.status = "completed"
    assert calls == []


def test_a_raising_subject_cannot_stop_the_task_ending_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def subject() -> str:
        msg = "cannot name this"
        raise ValueError(msg)

    state = _state()
    with (
        caplog.at_level(logging.WARNING),
        RefusalAwareTerminal(state, subject).record(),
    ):
        _raise(UnindexableContentError("binary content rejected: pdf header"))

    assert (state.status, state.error) == (
        "failed",
        "binary content rejected: pdf header",
    )
    [record] = caplog.records
    assert record.getMessage() == (
        "Background remember (unnamed) (task t-1) rejected: "
        "binary content rejected: pdf header"
    )


def test_complete_records_the_results() -> None:
    state = _state()
    terminal = RefusalAwareTerminal(state)
    with terminal.record():
        terminal.complete({"chunks": 3})
    assert (state.status, state.results, state.error) == (
        "completed",
        {"chunks": 3},
        "",
    )


def test_a_subject_that_raises_anything_falls_back_to_unnamed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def subject() -> str:
        msg = "not a ValueError"
        raise RuntimeError(msg)

    state = _state()
    with (
        caplog.at_level(logging.WARNING),
        RefusalAwareTerminal(state, subject).record(),
    ):
        _raise(UnindexableContentError("binary content rejected: pdf header"))

    assert state.status == "failed"
    [record] = caplog.records
    assert record.getMessage() == (
        "Background remember (unnamed) (task t-1) rejected: "
        "binary content rejected: pdf header"
    )


def test_a_row_budget_violation_is_a_refusal_not_a_fault(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _state()
    reason = "row budget exceeded: summary 8,193 bytes > 8,192"
    with caplog.at_level(logging.WARNING), RefusalAwareTerminal(state).record():
        _raise(RowBudgetError(reason))

    assert (state.status, state.error) == ("failed", reason)
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
