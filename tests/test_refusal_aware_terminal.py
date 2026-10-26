"""Tests for quarry.daemon.refusal_aware_terminal: a refusal ends a task quietly."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, cast

import pytest

from quarry.content_guard import UnindexableContentError
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.refusal_aware_terminal import RefusalAwareTerminal
from quarry.daemon.settlement import Settlement
from quarry.daemon.tasks import TaskState
from quarry.db.row_budget import RowBudgetError
from tests.job_states import job_in

if TYPE_CHECKING:
    from collections.abc import Callable

Kind = Literal["job", "task"]


def _raise(error: Exception) -> None:
    raise error


def _terminal(
    kind: Kind, subject: Callable[[], str] = str
) -> tuple[TaskState, RefusalAwareTerminal]:
    """Return a running state of *kind* and the terminal bound to it."""
    if kind == "job":
        state = job_in("running")
        return state, RefusalAwareTerminal.for_job(state, subject)
    state = TaskState(task_id="remember-t", kind="remember")
    return state, RefusalAwareTerminal.for_task(state, subject)


@pytest.fixture(params=["job", "task"])
def kind(request: pytest.FixtureRequest) -> Kind:
    """Both ways a refusal-aware terminal is built: for a queued job, for a task."""
    return cast("Kind", request.param)


def test_a_refusal_fails_the_task_with_one_warning_and_no_traceback(
    kind: Kind, caplog: pytest.LogCaptureFixture
) -> None:
    state, terminal = _terminal(kind)
    with caplog.at_level(logging.WARNING), terminal.record():
        _raise(UnindexableContentError("binary content rejected: pdf header"))

    assert (state.status, state.error) == (
        "failed",
        "binary content rejected: pdf header",
    )
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None


def test_any_other_error_is_still_logged_with_its_traceback(
    kind: Kind, caplog: pytest.LogCaptureFixture
) -> None:
    state, terminal = _terminal(kind)
    with caplog.at_level(logging.WARNING), terminal.record():
        _raise(RuntimeError("boom"))

    assert (state.status, state.error) == ("failed", "internal error: RuntimeError")
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None


def test_a_body_that_settles_its_own_status_is_left_alone(kind: Kind) -> None:
    state, terminal = _terminal(kind)
    with terminal.record():
        terminal.apply(Settlement.done({"chunks": 1}))
    assert state.status == "completed"


def test_a_body_that_sets_no_status_is_marked_failed(kind: Kind) -> None:
    state, terminal = _terminal(kind)
    with terminal.record():
        pass
    assert state.status == "failed"


def test_the_warning_names_the_task_and_the_subject(
    kind: Kind, caplog: pytest.LogCaptureFixture
) -> None:
    state, terminal = _terminal(kind, lambda: "note.md")
    with caplog.at_level(logging.WARNING), terminal.record():
        _raise(UnindexableContentError("binary content rejected: pdf header"))

    [record] = caplog.records
    assert record.getMessage() == (
        f"Background remember note.md (task {state.task_id}) rejected: "
        "binary content rejected: pdf header"
    )


def test_the_subject_is_built_only_when_a_refusal_is_logged(kind: Kind) -> None:
    calls: list[int] = []

    def subject() -> str:
        calls.append(1)
        return "x"

    _state, terminal = _terminal(kind, subject)
    with terminal.record():
        terminal.apply(Settlement.done({}))
    assert calls == []


def test_a_raising_subject_cannot_stop_the_task_ending_failed(
    kind: Kind, caplog: pytest.LogCaptureFixture
) -> None:
    def subject() -> str:
        msg = "cannot name this"
        raise ValueError(msg)

    state, terminal = _terminal(kind, subject)
    with caplog.at_level(logging.WARNING), terminal.record():
        _raise(UnindexableContentError("binary content rejected: pdf header"))

    assert (state.status, state.error) == (
        "failed",
        "binary content rejected: pdf header",
    )
    naming, record = caplog.records
    assert naming.levelno == logging.WARNING
    assert naming.getMessage() == (
        f"Background remember (task {state.task_id}): naming the subject failed"
    )
    assert record.getMessage() == (
        f"Background remember (unnamed) (task {state.task_id}) rejected: "
        "binary content rejected: pdf header"
    )


def test_a_store_with_rows_completes_the_task(kind: Kind) -> None:
    state, terminal = _terminal(kind)
    with terminal.record():
        terminal.apply(RefusalAwareTerminal.settlement({"chunks": 3}))
    assert (state.status, state.results, state.error) == (
        "completed",
        {"chunks": 3},
        "",
    )


@pytest.mark.parametrize(
    ("results", "reason"),
    [
        ({"chunks": 0}, RefusalAwareTerminal.NOTHING_STORED),
        ({"ingested": 0, "empty": True}, RefusalAwareTerminal.NOTHING_STORED),
        ({"ingested": 0}, RefusalAwareTerminal.NO_PAGES),
    ],
)
def test_a_store_that_wrote_nothing_is_refused_never_completed(
    kind: Kind, results: dict[str, object], reason: str
) -> None:
    state, terminal = _terminal(kind)
    with terminal.record():
        terminal.apply(RefusalAwareTerminal.settlement(results))
    assert (state.status, state.error) == ("failed", reason)


def test_a_subject_that_raises_anything_falls_back_to_unnamed(
    kind: Kind, caplog: pytest.LogCaptureFixture
) -> None:
    def subject() -> str:
        msg = "not a ValueError"
        raise RuntimeError(msg)

    state, terminal = _terminal(kind, subject)
    with caplog.at_level(logging.WARNING), terminal.record():
        _raise(UnindexableContentError("binary content rejected: pdf header"))

    assert state.status == "failed"
    naming, record = caplog.records
    assert naming.levelno == logging.WARNING
    assert naming.getMessage() == (
        f"Background remember (task {state.task_id}): naming the subject failed"
    )
    assert record.getMessage() == (
        f"Background remember (unnamed) (task {state.task_id}) rejected: "
        "binary content rejected: pdf header"
    )


def test_a_row_budget_violation_is_a_refusal_not_a_fault(
    kind: Kind, caplog: pytest.LogCaptureFixture
) -> None:
    state, terminal = _terminal(kind)
    reason = "row budget exceeded: summary 8,193 bytes > 8,192"
    with caplog.at_level(logging.WARNING), terminal.record():
        _raise(RowBudgetError(reason))

    assert (state.status, state.error) == ("failed", reason)
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None


def test_a_refusal_the_status_would_not_take_is_not_logged_as_a_rejection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = job_in("running")
    terminal = RefusalAwareTerminal.for_job(state)
    JobLifecycle(state).withdraw()  # the producer gave up: the job is aborted
    with caplog.at_level(logging.WARNING), terminal.record():
        _raise(UnindexableContentError("job withdrawn before its commit"))

    assert state.status == "aborted"
    assert caplog.records == []
