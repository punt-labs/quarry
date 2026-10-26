"""JobLifecycle: a job runs, commits or is withdrawn; a withdrawn one never writes."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Self, cast, final

import pytest

from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.owner_lock import ClaimNotHeldError
from quarry.daemon.settlement import Settlement
from quarry.daemon.tasks import TaskState
from tests.job_states import JobStates

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.owner_lock import Claim
    from quarry.daemon.store_run import StoreRun
    from quarry.daemon.tasks import TaskStatus

_CTX = cast("DaemonContext", SimpleNamespace())
_Withdrawn = JobLifecycle.Withdrawn


def _state(status: TaskStatus) -> TaskState:
    return TaskState(task_id="j", kind="backfill", status=status)


@final
class _Job:
    """A job that settles itself and counts how often it ran."""

    __slots__ = ("_ran",)

    _ran: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._ran = 0
        return self

    @property
    def ran(self) -> int:
        return self._ran

    async def run(self, ctx: object, state: TaskState) -> None:
        del ctx
        self._ran += 1
        JobLifecycle(state).settle_stored({})


def _queued() -> TaskState:
    state = _state("new")
    moved = JobLifecycle(state).enqueue()
    assert moved
    return state


def test_a_queued_job_starts_once() -> None:
    state = _state("queued")

    moved = JobLifecycle(state).start()
    assert moved
    assert state.status == "running"
    moved = JobLifecycle(state).start()
    assert not moved


def test_a_job_withdrawn_before_it_was_queued_stays_withdrawn() -> None:
    state = _state("new")
    JobLifecycle(state).withdraw()

    moved = JobLifecycle(state).enqueue()
    assert not moved
    assert state.status == "withdrawn"


@pytest.mark.parametrize("status", ["running", "committing", "completed", "aborted"])
def test_starting_a_job_in_any_other_status_is_logged_at_warning(
    status: TaskStatus, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="quarry.daemon.job_lifecycle"):
        moved = JobLifecycle(_state(status)).start()
        assert not moved

    assert status in caplog.text


def test_starting_a_withdrawn_job_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    state = _state("queued")
    JobLifecycle(state).withdraw()

    with caplog.at_level(logging.WARNING, logger="quarry.daemon.job_lifecycle"):
        moved = JobLifecycle(state).start()
        assert not moved

    assert caplog.text == ""


def test_a_job_withdrawn_before_it_runs_never_starts_and_says_so() -> None:
    state = _state("queued")

    verdict = JobLifecycle(state).withdraw()
    assert verdict is _Withdrawn.NEVER_STARTED
    verdict = JobLifecycle(state).withdraw()
    assert verdict is _Withdrawn.NEVER_STARTED  # still
    moved = JobLifecycle(state).start()
    assert not moved


def test_a_job_that_was_never_queued_is_withdrawn_too() -> None:
    verdict = JobLifecycle(_state("new")).withdraw()
    assert verdict is _Withdrawn.NEVER_STARTED


def test_a_job_withdrawn_while_running_stops_at_its_commit() -> None:
    state = _state("queued")
    moved = JobLifecycle(state).start()
    assert moved

    verdict = JobLifecycle(state).withdraw()
    assert verdict is _Withdrawn.STARTED
    moved = JobLifecycle(state).commit()
    assert not moved  # it writes nothing
    verdict = JobLifecycle(state).withdraw()
    assert verdict is _Withdrawn.STARTED  # and still says it ran


def test_a_job_past_its_commit_cannot_be_withdrawn() -> None:
    state = _state("queued")
    moved = JobLifecycle(state).start()
    assert moved

    moved = JobLifecycle(state).commit()
    assert moved
    verdict = JobLifecycle(state).withdraw()
    assert verdict is _Withdrawn.COMMITTING
    assert state.status == "committing"


@pytest.mark.parametrize(
    ("status", "expected"),
    [("completed", _Withdrawn.STORED), ("failed", _Withdrawn.FAILED)],
)
def test_a_finished_job_is_reported_as_it_finished(
    status: TaskStatus, expected: JobLifecycle.Withdrawn
) -> None:
    state = _state(status)

    verdict = JobLifecycle(state).withdraw()
    assert verdict is expected
    assert state.status == status


async def test_a_queued_job_runs_under_the_gate() -> None:
    state, job = _queued(), _Job()

    await JobLifecycle(state).run(cast("IngestUnit", job), _CTX, asyncio.Semaphore(1))

    assert (job.ran, state.status) == (1, "completed")


async def test_a_job_withdrawn_before_the_gate_is_skipped_without_taking_it() -> None:
    state, job = _queued(), _Job()
    JobLifecycle(state).withdraw()
    gate = asyncio.Semaphore(0)  # a held gate: taking it would hang

    await asyncio.wait_for(
        JobLifecycle(state).run(cast("IngestUnit", job), _CTX, gate), timeout=2
    )

    assert job.ran == 0


async def test_a_job_withdrawn_while_it_waits_for_the_gate_is_skipped_at_the_gate() -> (
    None
):
    state, job = _queued(), _Job()
    gate = asyncio.Semaphore(0)
    running = asyncio.create_task(
        JobLifecycle(state).run(cast("IngestUnit", job), _CTX, gate)
    )
    await asyncio.sleep(0.05)  # it is queued, waiting on the gate

    JobLifecycle(state).withdraw()
    gate.release()
    await asyncio.wait_for(running, timeout=2)

    assert job.ran == 0
    assert state.status == "withdrawn"


async def test_a_job_that_is_not_queued_is_not_run_twice() -> None:
    state, job = _queued(), _Job()
    moved = JobLifecycle(state).start()
    assert moved  # already dispatched once

    await JobLifecycle(state).run(cast("IngestUnit", job), _CTX, asyncio.Semaphore(1))

    assert job.ran == 0


async def test_a_job_that_is_not_queued_warns_when_it_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state, job = _queued(), _Job()
    moved = JobLifecycle(state).start()
    assert moved

    with caplog.at_level(logging.WARNING, logger="quarry.daemon.job_lifecycle"):
        await JobLifecycle(state).run(
            cast("IngestUnit", job), _CTX, asyncio.Semaphore(1)
        )

    assert "running" in caplog.text


def _job(status: TaskStatus) -> TaskState:
    return JobStates.reaching(status, kind="backfill")


def _snapshot(state: TaskState) -> tuple[str, str, dict[str, object]]:
    """Return what a move must leave alone: the status, the reason, the results."""
    return state.status, state.error, dict(state.results)


@pytest.mark.parametrize("status", ["running", "committing"])
def test_settle_stored_completes_a_job_the_worker_owns_and_clears_its_reason(
    status: TaskStatus,
) -> None:
    state = _job(status)
    state.error = "daemon shut down before ingest"

    moved = JobLifecycle(state).settle_stored({"chunks": 2})
    assert moved

    assert (state.status, state.error, state.results) == (
        "completed",
        "",
        {"chunks": 2},
    )


@pytest.mark.parametrize("status", ["withdrawn", "aborted", "completed", "failed"])
def test_settle_never_overwrites_a_status_somebody_else_settled(
    status: TaskStatus,
) -> None:
    state = _job(status)
    before = _snapshot(state)

    moved = JobLifecycle(state).settle_stored({"chunks": 2})
    assert not moved
    moved = JobLifecycle(state).settle_failed("late")
    assert not moved

    assert _snapshot(state) == before


@pytest.mark.parametrize("status", ["running", "committing"])
def test_settle_failed_fails_a_job_the_worker_owns(status: TaskStatus) -> None:
    state = _job(status)

    moved = JobLifecycle(state).settle_failed("refused")
    assert moved

    assert (state.status, state.error) == ("failed", "refused")


def test_a_cancel_fails_a_running_job_but_leaves_the_others() -> None:
    kept = {s: _job(s) for s in ("committing", "aborted", "completed", "withdrawn")}
    running = _job("running")

    for state in (running, *kept.values()):
        JobLifecycle(state).cancelled("task was cancelled")

    assert (running.status, running.error) == ("failed", "task was cancelled")
    assert {s: state.status for s, state in kept.items()} == {s: s for s in kept}


def test_an_exit_with_no_terminal_status_fails_only_a_running_job() -> None:
    running, aborted = _job("running"), _job("aborted")

    for state in (running, aborted):
        JobLifecycle(state).exited("task exited without setting terminal status")

    assert running.status == "failed"
    assert aborted.status == "aborted"


@pytest.mark.parametrize(
    ("status", "disposition"),
    [
        ("withdrawn", JobLifecycle.Disposition.KEPT),
        ("aborted", JobLifecycle.Disposition.KEPT),
        ("completed", JobLifecycle.Disposition.KEPT),
        ("committing", JobLifecycle.Disposition.UNCONFIRMED),
        ("queued", JobLifecycle.Disposition.FAILED),
        ("running", JobLifecycle.Disposition.FAILED),
        ("failed", JobLifecycle.Disposition.FAILED),
    ],
)
def test_the_abort_fails_only_what_it_owns(
    status: TaskStatus, disposition: JobLifecycle.Disposition
) -> None:
    state = _job(status)

    moved = JobLifecycle(state).fail_aborted("shut down")
    assert moved is disposition

    failed = disposition is JobLifecycle.Disposition.FAILED
    assert (state.status == "failed") is failed
    assert (state.error == "shut down") is failed


def test_observing_a_store_that_landed_completes_a_failed_or_committing_job() -> None:
    for status in ("failed", "committing"):
        state = _job(status)
        state.error = "daemon shut down before ingest"

        observed = JobLifecycle(state).observe_store(Settlement.done({"chunks": 2}))

        assert observed is JobLifecycle.Observed.COMPLETED
        assert (state.status, state.error, state.results) == (
            "completed",
            "",
            {"chunks": 2},
        )


def test_observing_a_failed_store_fails_a_committing_job_only() -> None:
    committing = _job("committing")

    observed = JobLifecycle(committing).observe_store(
        Settlement.failed("insert failed")
    )

    assert observed is JobLifecycle.Observed.FAILED
    assert (committing.status, committing.error) == ("failed", "insert failed")


@pytest.mark.parametrize("status", ["running", "withdrawn", "aborted", "completed"])
def test_observing_a_store_leaves_a_status_somebody_else_settled(
    status: TaskStatus,
) -> None:
    for settlement in (Settlement.done({"chunks": 2}), Settlement.failed("no")):
        state = _job(status)
        before = _snapshot(state)

        observed = JobLifecycle(state).observe_store(settlement)

        assert observed is JobLifecycle.Observed.UNCHANGED
        assert _snapshot(state) == before


def test_observing_a_failed_store_leaves_a_failed_job_and_its_reason() -> None:
    state = _job("failed")
    state.error = "kept"

    observed = JobLifecycle(state).observe_store(Settlement.failed("later"))

    assert observed is JobLifecycle.Observed.UNCHANGED
    assert state.error == "kept"


@final
class _Store:
    """A store double: it has ended with *outcome* or still runs; it is observed.

    It records whether its reader held the job's claim when it read the outcome.
    """

    __slots__ = ("_claim", "_detached", "_outcome", "_reads")

    _claim: Claim
    _detached: bool
    _outcome: Settlement | None  # None: the store still runs
    _reads: list[bool]

    def __new__(cls, outcome: Settlement | None, claim: Claim) -> Self:
        self = super().__new__(cls)
        self._claim = claim
        self._detached = False
        self._outcome = outcome
        self._reads = []
        return self

    @property
    def detached(self) -> bool:
        return self._detached

    @property
    def reads_under_the_claim(self) -> list[bool]:
        return list(self._reads)

    def outcome(self) -> Settlement | None:
        self._reads.append(self._claim.owned())
        return self._outcome

    def detach(self) -> None:
        self._detached = True


def _cancelled(
    status: TaskStatus, outcome: Settlement | None
) -> tuple[TaskState, _Store]:
    state = _job(status)
    store = _Store(outcome, state.claim)
    state.store = cast("StoreRun", store)
    JobLifecycle(state).cancelled("task was cancelled")
    return state, store


@pytest.mark.parametrize("status", ["running", "committing"])
def test_a_cancel_after_the_store_landed_completes_the_job(
    status: TaskStatus,
) -> None:
    state, store = _cancelled(status, Settlement.done({"chunks": 1}))

    assert (state.status, state.results) == ("completed", {"chunks": 1})
    assert store.detached


@pytest.mark.parametrize(
    ("status", "outcome", "want"),
    [
        ("running", None, "failed"),
        ("committing", None, "committing"),
        ("running", Settlement.failed("no"), "failed"),
        ("committing", Settlement.failed("no"), "failed"),
        ("aborted", None, "aborted"),
        ("withdrawn", None, "withdrawn"),
    ],
)
def test_a_cancel_fails_only_a_job_whose_store_did_not_land(
    status: TaskStatus, outcome: Settlement | None, want: str
) -> None:
    state, store = _cancelled(status, outcome)

    assert state.status == want
    assert store.detached


@pytest.mark.parametrize("status", ["running", "committing"])
def test_a_cancel_after_a_refused_store_keeps_the_stores_own_reason(
    status: TaskStatus,
) -> None:
    state, _store = _cancelled(status, Settlement.failed("nothing was stored: blank"))

    assert (state.status, state.error) == ("failed", "nothing was stored: blank")


def test_a_job_its_own_store_refused_keeps_the_refusal_when_the_abort_takes_it() -> (
    None
):
    state, _store = _cancelled("running", Settlement.failed("blank"))

    taken = JobLifecycle(state).take_for_abort("shut down")

    assert taken is JobLifecycle.Disposition.SETTLED
    assert (state.status, state.error) == ("failed", "blank")


def test_a_job_the_abort_failed_is_taken_again_as_settled_when_its_store_refused() -> (
    None
):
    state = _job("running")
    store = _Store(Settlement.failed("blank"), state.claim)
    state.store = cast("StoreRun", store)
    JobLifecycle(state).fail_aborted("shut down")  # the abort's own provisional

    again = JobLifecycle(state).take_for_abort("shut down")

    assert again is JobLifecycle.Disposition.SETTLED  # the refusal outranks it


def test_a_failed_job_with_no_store_is_the_aborts_to_fail() -> None:
    state = _job("failed")

    taken = JobLifecycle(state).take_for_abort("shut down")

    assert taken is JobLifecycle.Disposition.FAILED
    assert state.error == "shut down"


@final
class _UnheldClaim:
    """A claim whose holder is never the caller: a write that took it is a bug."""

    __slots__ = ()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def owned(self) -> bool:
        return False


def test_a_write_without_the_claim_raises_and_changes_nothing() -> None:
    state = _job("running")
    state.claim = _UnheldClaim()

    with pytest.raises(ClaimNotHeldError):
        JobLifecycle(state).settle_failed("sneaked")

    assert (state.status, state.error) == ("running", "")


@pytest.mark.parametrize("status", ["running", "committing"])
def test_a_cancel_reads_the_stores_outcome_under_the_claim(
    status: TaskStatus,
) -> None:
    _, store = _cancelled(status, Settlement.failed("no"))

    assert store.reads_under_the_claim == [True]


def test_a_settle_the_status_refused_is_logged_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _job("aborted")

    with caplog.at_level(logging.DEBUG, logger="quarry.daemon.job_lifecycle"):
        moved = JobLifecycle(state).settle_failed("late")
        assert not moved
        moved = JobLifecycle(state).settle_stored({})
        assert not moved

    assert "not failed, its status is aborted" in caplog.text
    assert "not completed, its status is aborted" in caplog.text
