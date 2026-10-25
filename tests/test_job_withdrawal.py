"""JobWithdrawal: a withdrawn job never writes; one past its commit is stored."""

from __future__ import annotations

import pytest

from quarry.daemon.job_claim import JobClaim
from quarry.daemon.job_withdrawal import JobWithdrawal
from quarry.daemon.tasks import TaskState
from quarry.daemon.withdrawn import Withdrawn


def _state(status: str) -> TaskState:
    return TaskState(task_id="j", kind="backfill", status=status)


def test_a_job_withdrawn_before_it_runs_never_starts_and_says_so() -> None:
    state = _state("queued")

    assert JobWithdrawal(state).withdraw() is Withdrawn.NEVER_STARTED
    assert JobWithdrawal(state).withdraw() is Withdrawn.NEVER_STARTED  # still
    assert not JobClaim(state).start()


def test_a_job_that_was_never_submitted_can_be_withdrawn() -> None:
    assert JobWithdrawal(_state("new")).withdraw() is Withdrawn.NEVER_STARTED


def test_a_job_withdrawn_while_it_runs_stops_at_its_commit() -> None:
    state = _state("queued")
    assert JobClaim(state).start()

    assert JobWithdrawal(state).withdraw() is Withdrawn.STARTED
    assert not JobWithdrawal(state).commit()  # it writes nothing
    assert JobWithdrawal(state).withdraw() is Withdrawn.STARTED  # and still says it ran


def test_a_job_that_committed_can_no_longer_be_withdrawn() -> None:
    state = _state("queued")
    assert JobClaim(state).start()

    assert JobWithdrawal(state).commit()
    assert JobWithdrawal(state).withdraw() is Withdrawn.COMMITTING
    assert state.status == "committing"


@pytest.mark.parametrize(
    ("status", "verdict"),
    [("completed", Withdrawn.STORED), ("failed", Withdrawn.FAILED)],
)
def test_a_job_that_already_finished_is_counted_as_it_finished(
    status: str, verdict: Withdrawn
) -> None:
    state = _state(status)

    assert JobWithdrawal(state).withdraw() is verdict
    assert state.status == status


def test_a_job_in_a_status_it_does_not_know_is_not_claimed_untouched() -> None:
    assert JobWithdrawal(_state("mystery")).withdraw() is Withdrawn.STARTED
