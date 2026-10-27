"""Tests for quarry.reported_error and how a task's failure text reads it."""

from __future__ import annotations

import pytest

from quarry.daemon.tasks import TaskState
from quarry.daemon.terminal import Terminal
from quarry.failure_reason import FailureReason
from quarry.refusal import RefusalError
from quarry.reported_error import ReportedError


class TestReportedAs:
    def test_a_class_name_is_reported_as_it_is(self) -> None:
        assert ReportedError("ValueError").reported_as == "ValueError"

    @pytest.mark.parametrize(
        "name",
        ["", "has space", "a.b", "x" * 65, "1Leading", "http://h/?t=SECRET", "a\\nb"],
    )
    def test_anything_that_is_not_an_identifier_falls_back_to_sitemap(
        self, name: str
    ) -> None:
        assert ReportedError(name).reported_as == "sitemap"

    def test_no_argument_falls_back_to_sitemap(self) -> None:
        assert ReportedError().reported_as == "sitemap"


class TestTheFailureText:
    def test_a_refusal_is_its_own_message(self) -> None:
        assert FailureReason.of(RefusalError("no")) == "no"

    def test_a_reported_error_reads_its_class(self) -> None:
        assert (
            FailureReason.of(ReportedError("TypeError")) == "internal error: TypeError"
        )

    def test_any_other_exception_reads_its_own_class(self) -> None:
        assert FailureReason.of(KeyError("k")) == "internal error: KeyError"


def _fail(exc: Exception) -> None:
    raise exc


class TestTheTerminal:
    def test_a_reported_error_ends_the_task_with_the_recorded_class(self) -> None:
        state = TaskState(task_id="t", kind="ingest")
        with Terminal.for_task(state).recording():
            _fail(ReportedError("ValueError"))
        assert (state.status, state.error) == ("failed", "internal error: ValueError")

    def test_a_plain_exception_ends_the_task_with_its_class(self) -> None:
        state = TaskState(task_id="t", kind="ingest")
        with Terminal.for_task(state).recording():
            _fail(KeyError("k"))
        assert state.error == "internal error: KeyError"
