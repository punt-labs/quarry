"""AbortRecord: a taken job's reason and term move together, once."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast, final

from quarry.daemon.abort_reason import Aborted, AbortReason
from quarry.daemon.abort_record import AbortRecord
from quarry.daemon.abort_tally import AbortTally, AbortTerm
from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.job_spool import SpoolRecord
from quarry.daemon.queued_unit import QueuedUnit
from quarry.daemon.settlement import Settlement
from quarry.daemon.spool_discard import SpoolDiscard
from quarry.daemon.store_run import StoreRun
from tests.job_states import JobStates

if TYPE_CHECKING:
    import pytest

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.job_spool import JobSpool
    from quarry.daemon.tasks import TaskState, TaskStatus


@final
@dataclass(frozen=True, slots=True)
class _Durable:
    """A job whose content is held elsewhere: it has no spool record."""

    collection: str = "c"

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        del ctx, state

    def spool_record(self) -> SpoolRecord | None:
        return None


@final
@dataclass(frozen=True, slots=True)
class _Fragile:
    """A job whose only copy is the one the abort spools."""

    collection: str = "c"

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        del ctx, state

    def spool_record(self) -> SpoolRecord | None:
        return SpoolRecord("remember", "c", "name", "payload")


@final
class _NoSpool:
    """A spool that is never asked: these jobs spool nothing."""

    __slots__ = ()

    def write(self, record: SpoolRecord) -> Path | None:
        del record
        return None


def _record() -> tuple[AbortRecord, AbortTally, list[QueuedUnit]]:
    """Return a record, its tally, and the jobs it asked to have spooled again."""
    tally = AbortTally()
    respooled: list[QueuedUnit] = []
    reason = AbortReason(cast("JobSpool", _NoSpool()))
    discard = SpoolDiscard(DaemonThreads(1, "t"))
    return AbortRecord(reason, tally, discard, respooled.append), tally, respooled


def _unit(status: TaskStatus, *, durable: bool = True) -> QueuedUnit:
    return QueuedUnit(_Durable() if durable else _Fragile(), JobStates.reaching(status))


def test_take_fails_what_it_may_and_counts_the_rest_where_they_belong() -> None:
    record, tally, _ = _record()
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
    assert record.taken == (queued, withdrawn, committing)


def test_a_job_taken_twice_is_failed_and_counted_once() -> None:
    record, tally, _ = _record()
    unit = _unit("queued")

    assert record.take([unit]) == [unit]
    assert record.take([unit]) == []
    assert tally.summary().startswith("0 job(s)")
    assert tally.pending == 1
    assert record.taken == (unit,)


async def test_a_job_its_own_store_refused_keeps_the_refusal_and_needs_no_spool() -> (
    None
):
    record, tally, _ = _record()
    unit = _unit("running", durable=False)
    store = StoreRun.start(
        unit.job,
        unit.state,
        dict,
        lambda _results: Settlement.failed("nothing was stored: blank"),
    )
    await store.wait()
    JobLifecycle(unit.state).cancelled("task was cancelled")

    record.take([unit])

    assert tally.term_of(unit) is AbortTerm.NO_SPOOL
    assert unit.state.error == "nothing was stored: blank"
    assert tally.pending == 0


def test_an_outcome_moves_the_reason_and_the_term_together_while_pending() -> None:
    record, tally, _ = _record()
    unit = _unit("queued")
    record.take([unit])

    record.record(unit, Aborted("spooled for recovery", AbortTerm.SPOOLED))

    assert unit.state.error == "spooled for recovery"
    assert tally.term_of(unit) is AbortTerm.SPOOLED


def test_an_outcome_for_a_job_no_longer_pending_changes_nothing() -> None:
    record, tally, _ = _record()
    unit = _unit("queued")
    record.take([unit])
    record.record(unit, Aborted("first", AbortTerm.SPOOLED))

    record.record(unit, Aborted("second", AbortTerm.NOT_WRITTEN))

    assert unit.state.error == "first"
    assert tally.term_of(unit) is AbortTerm.SPOOLED


def test_a_store_that_landed_makes_the_job_kept_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    record, tally, _ = _record()
    unit = _unit("committing")
    record.take([unit])

    with caplog.at_level(logging.INFO, logger="quarry.daemon"):
        record.store_ended(unit, JobLifecycle.Observed.COMPLETED)

    assert tally.term_of(unit) is AbortTerm.KEPT
    assert f"(task {unit.state.task_id}) is now kept: its store landed" in caplog.text


def test_a_committing_store_that_failed_needs_no_spool_and_keeps_its_own_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    record, tally, respooled = _record()
    unit = _unit("committing")
    record.take([unit])
    JobLifecycle(unit.state).observe_store(Settlement.failed("insert failed"))

    with caplog.at_level(logging.INFO, logger="quarry.daemon"):
        record.store_ended(unit, JobLifecycle.Observed.FAILED)

    assert tally.term_of(unit) is AbortTerm.NO_SPOOL
    assert unit.state.error == "insert failed"  # the store's reason, not the abort's
    assert respooled == []
    assert "is now no_spool: its committing store failed" in caplog.text


def test_a_committing_job_nothing_durable_holds_is_spooled_when_its_store_fails() -> (
    None
):
    record, tally, respooled = _record()
    unit = _unit("committing", durable=False)
    record.take([unit])
    JobLifecycle(unit.state).observe_store(Settlement.failed("insert failed"))

    record.store_ended(unit, JobLifecycle.Observed.FAILED)

    assert tally.term_of(unit) is AbortTerm.PENDING  # until its spool is recorded
    assert respooled == [unit]
    assert unit.state.status == "failed"


def test_an_unchanged_store_and_an_untaken_job_change_nothing() -> None:
    record, tally, respooled = _record()
    unit, stranger = _unit("committing"), _unit("committing")
    record.take([unit])

    record.store_ended(unit, JobLifecycle.Observed.UNCHANGED)
    record.store_ended(stranger, JobLifecycle.Observed.COMPLETED)

    assert tally.term_of(unit) is AbortTerm.UNCONFIRMED
    assert tally.term_of(stranger) is None
    assert respooled == []


async def test_a_late_spool_for_a_job_that_stored_is_removed_not_counted(
    tmp_path: Path,
) -> None:
    record, tally, _ = _record()
    unit = _unit("queued")
    record.take([unit])
    record.record(unit, AbortReason.UNFINISHED)
    written = tmp_path / "remember-1.json"
    written.write_text("{}")
    spooling: asyncio.Future[Aborted] = asyncio.get_running_loop().create_future()
    spooling.set_result(Aborted("spooled", AbortTerm.SPOOLED, written))
    JobLifecycle(unit.state).observe_store(Settlement.done({"chunks": 1}))
    record.store_ended(unit, JobLifecycle.Observed.COMPLETED)

    record.late(unit, spooling)
    for _ in range(100):
        if not written.exists():
            break
        await asyncio.sleep(0.01)

    assert not written.exists()
    assert tally.term_of(unit) is AbortTerm.KEPT


async def test_a_late_spool_that_raised_settles_the_job_as_not_written() -> None:
    record, tally, _ = _record()
    unit = _unit("queued")
    record.take([unit])
    record.record(unit, AbortReason.UNFINISHED)
    spooling: asyncio.Future[Aborted] = asyncio.get_running_loop().create_future()
    spooling.set_exception(OSError("disk"))

    record.late(unit, spooling)

    assert tally.term_of(unit) is AbortTerm.NOT_WRITTEN
    assert "spool write failed" in unit.state.error


async def test_a_late_spool_that_was_cancelled_leaves_the_job_unfinished() -> None:
    record, tally, _ = _record()
    unit = _unit("queued")
    record.take([unit])
    record.record(unit, AbortReason.UNFINISHED)
    spooling: asyncio.Future[Aborted] = asyncio.get_running_loop().create_future()
    spooling.cancel()

    record.late(unit, spooling)

    assert tally.term_of(unit) is AbortTerm.UNFINISHED


async def test_a_late_outcome_for_a_job_that_is_not_unfinished_changes_nothing() -> (
    None
):
    record, tally, _ = _record()
    unit = _unit("queued")
    record.take([unit])
    record.record(unit, Aborted("spooled", AbortTerm.SPOOLED))
    spooling: asyncio.Future[Aborted] = asyncio.get_running_loop().create_future()
    spooling.set_result(Aborted("lost", AbortTerm.NOT_WRITTEN))

    record.late(unit, spooling)

    assert tally.term_of(unit) is AbortTerm.SPOOLED
