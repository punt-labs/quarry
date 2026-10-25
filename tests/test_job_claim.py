"""JobClaim: a job is run, committed or withdrawn, and a withdrawn one never writes."""

from __future__ import annotations

import logging

import pytest

from quarry.daemon.job_claim import JobClaim
from quarry.daemon.job_withdrawal import JobWithdrawal
from quarry.daemon.tasks import TaskState


def _state(status: str) -> TaskState:
    return TaskState(task_id="j", kind="backfill", status=status)


def test_a_queued_job_runs_once() -> None:
    state = _state("queued")

    assert JobClaim(state).start()
    assert state.status == "running"
    assert not JobClaim(state).start()


def test_a_job_withdrawn_before_it_was_queued_stays_withdrawn() -> None:
    state = _state("new")
    JobWithdrawal(state).withdraw()

    assert not JobClaim(state).enqueue()
    assert state.status == "withdrawn"


@pytest.mark.parametrize("status", ["running", "committing", "completed", "aborted"])
def test_starting_a_job_in_any_other_status_is_logged_at_warning(
    status: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="quarry.daemon.job_claim"):
        assert not JobClaim(_state(status)).start()

    assert status in caplog.text


def test_starting_a_withdrawn_job_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    state = _state("queued")
    JobWithdrawal(state).withdraw()

    with caplog.at_level(logging.WARNING, logger="quarry.daemon.job_claim"):
        assert not JobClaim(state).start()

    assert caplog.text == ""
