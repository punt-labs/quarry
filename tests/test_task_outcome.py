"""Tests for quarry.daemon.task_outcome: a refusal ends a task quietly."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from quarry.content_guard import UnindexableContentError
from quarry.daemon.task_outcome import TaskOutcome
from quarry.daemon.tasks import TaskState

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
    with caplog.at_level(logging.WARNING), TaskOutcome(state).record():
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
    with caplog.at_level(logging.WARNING), TaskOutcome(state).record():
        _raise(RuntimeError("boom"))

    assert (state.status, state.error) == ("failed", "boom")
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None


def test_a_body_that_sets_its_own_status_is_left_alone() -> None:
    state = _state()
    with TaskOutcome(state).record():
        state.status = "completed"
    assert state.status == "completed"


def test_a_body_that_sets_no_status_is_marked_failed() -> None:
    state = _state()
    with TaskOutcome(state).record():
        pass
    assert state.status == "failed"
