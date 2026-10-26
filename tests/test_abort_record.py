"""AbortRecord: a taken job's reason and term move together, once."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast, final

from quarry.daemon.abort_reason import Aborted, AbortReason
from quarry.daemon.abort_record import AbortRecord
from quarry.daemon.abort_tally import AbortTally, AbortTerm
from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.queued_unit import QueuedUnit
from quarry.daemon.spool_discard import SpoolDiscard
from quarry.daemon.tasks import TaskState

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.job_spool import JobSpool, SpoolRecord
    from quarry.daemon.tasks import TaskStatus


@final
@dataclass(frozen=True, slots=True)
class _Unit:
    collection: str = "c"

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        del ctx, state

    def spool_record(self) -> SpoolRecord | None:
        return None


def _record() -> tuple[AbortRecord, AbortTally]:
    tally = AbortTally()
    reason = AbortReason(cast("JobSpool", _NoSpool()))
    return AbortRecord(reason, tally, SpoolDiscard(DaemonThreads(1, "t"))), tally


class _NoSpool:
    """A spool that is never asked: these jobs spool nothing."""

    def write(self, record: SpoolRecord) -> Path | None:
        del record
        return None


def _unit(status: TaskStatus) -> QueuedUnit:
    state = TaskState(task_id="j", kind="remember", status="new")
    assert JobLifecycle(state).enqueue()
    state.status = status
    return QueuedUnit(_Unit(), state)


def test_take_fails_what_it_may_and_counts_the_rest_where_they_belong() -> None:
    record, tally = _record()
    queued, withdrawn, committing = (
        _unit("queued"),
        _unit("withdrawn"),
        _unit("committing"),
    )

    failing = record.take([queued, withdrawn, committing])

    assert failing == [queued]
    assert queued.state.status == "failed"
    assert tally.term_of(queued) is AbortTerm.PENDING
    assert tally.term_of(withdrawn) is AbortTerm.KEPT
    assert tally.term_of(committing) is AbortTerm.UNCONFIRMED


def test_a_job_taken_twice_is_failed_and_counted_once() -> None:
    record, tally = _record()
    unit = _unit("queued")

    assert record.take([unit]) == [unit]
    assert record.take([unit]) == []
    assert tally.summary().startswith("0 job(s)")
    assert tally.pending == 1


def test_an_outcome_moves_the_reason_and_the_term_together_while_pending() -> None:
    record, tally = _record()
    unit = _unit("queued")
    record.take([unit])

    record.record(unit, Aborted("spooled for recovery", AbortTerm.SPOOLED))

    assert unit.state.error == "spooled for recovery"
    assert tally.term_of(unit) is AbortTerm.SPOOLED


def test_an_outcome_for_a_job_no_longer_pending_changes_nothing() -> None:
    record, tally = _record()
    unit = _unit("queued")
    record.take([unit])
    record.record(unit, Aborted("first", AbortTerm.SPOOLED))

    record.record(unit, Aborted("second", AbortTerm.LOST))

    assert unit.state.error == "first"
    assert tally.term_of(unit) is AbortTerm.SPOOLED


def test_a_store_that_landed_makes_the_job_kept() -> None:
    record, tally = _record()
    unit = _unit("committing")
    record.take([unit])

    record.store_ended(unit, JobLifecycle.Observed.COMPLETED)

    assert tally.term_of(unit) is AbortTerm.KEPT


def test_a_committing_store_that_failed_needs_no_spool_and_says_how_to_rerun() -> None:
    record, tally = _record()
    unit = _unit("committing")
    record.take([unit])
    unit.state.status = "failed"  # the observer has failed the job

    record.store_ended(unit, JobLifecycle.Observed.FAILED)

    assert tally.term_of(unit) is AbortTerm.NO_SPOOL
    assert unit.state.error == "daemon shut down before ingest completed"


def test_an_unchanged_store_and_an_untaken_job_change_nothing() -> None:
    record, tally = _record()
    unit, stranger = _unit("committing"), _unit("committing")
    record.take([unit])

    record.store_ended(unit, JobLifecycle.Observed.UNCHANGED)
    record.store_ended(stranger, JobLifecycle.Observed.COMPLETED)

    assert tally.term_of(unit) is AbortTerm.UNCONFIRMED
    assert tally.term_of(stranger) is None


async def test_a_late_spool_for_a_job_that_stored_is_removed_not_counted(
    tmp_path: Path,
) -> None:
    record, tally = _record()
    unit = _unit("queued")
    record.take([unit])
    record.record(unit, AbortReason.UNFINISHED)
    written = tmp_path / "remember-1.json"
    written.write_text("{}")
    spooling: asyncio.Future[Aborted] = asyncio.get_running_loop().create_future()
    spooling.set_result(Aborted("spooled", AbortTerm.SPOOLED, written))
    unit.state.status = "completed"
    record.store_ended(unit, JobLifecycle.Observed.COMPLETED)

    record.late(unit, spooling)
    for _ in range(100):
        if not written.exists():
            break
        await asyncio.sleep(0.01)

    assert not written.exists()
    assert tally.term_of(unit) is AbortTerm.KEPT


def test_a_late_outcome_for_a_job_that_is_not_unfinished_changes_nothing() -> None:
    record, tally = _record()
    unit = _unit("queued")
    record.take([unit])
    record.record(unit, Aborted("spooled", AbortTerm.SPOOLED))
    loop = asyncio.new_event_loop()
    spooling: asyncio.Future[Aborted] = loop.create_future()
    spooling.set_result(Aborted("lost", AbortTerm.LOST))

    record.late(unit, spooling)
    loop.close()

    assert tally.term_of(unit) is AbortTerm.SPOOLED
