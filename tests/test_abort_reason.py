"""AbortReason: what a drain-aborted job's terminal error says, and what it spools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, cast, final

from quarry.daemon.abort_reason import Aborted, AbortReason, AbortTally
from quarry.daemon.job_spool import JobSpool, SpoolRecord

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.tasks import TaskState


class _Spool:
    """A spool double: writes succeed or fail, and every record is kept."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.written: list[SpoolRecord] = []

    def write(self, record: SpoolRecord) -> bool:
        self.written.append(record)
        return self.ok


@final
@dataclass(frozen=True, slots=True)
class _Unit:
    record: SpoolRecord | None
    collection: str = "c"

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        del ctx, state

    def spool_record(self) -> SpoolRecord | None:
        return self.record


@final
@dataclass(frozen=True, slots=True)
class _Rerunnable:
    collection: str = "c"
    rerun_hint: ClassVar[str] = "re-run the thing"

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        del ctx, state

    def spool_record(self) -> SpoolRecord | None:
        return None


def _outcome(spool: _Spool, unit: _Unit | _Rerunnable) -> Aborted:
    return AbortReason(cast("JobSpool", spool)).of(unit)


def _reason(spool: _Spool, unit: _Unit | _Rerunnable) -> str:
    return _outcome(spool, unit).reason


def test_a_unit_with_a_durable_source_says_how_to_run_it_again() -> None:
    spool = _Spool()

    assert (
        _reason(spool, _Rerunnable())
        == "daemon shut down before ingest completed; re-run the thing"
    )
    assert spool.written == []


def test_a_unit_with_no_record_has_a_durable_copy_elsewhere() -> None:
    assert _reason(_Spool(), _Unit(None)) == "daemon shut down before ingest completed"


def test_a_spooled_record_is_recoverable_only_if_the_write_succeeded() -> None:
    record = SpoolRecord(kind="remember", collection="c", name="n", payload="p")

    ok = _reason(_Spool(ok=True), _Unit(record))
    failed = _reason(_Spool(ok=False), _Unit(record))

    assert ok == "daemon shut down before ingest; spooled for recovery"
    assert "spool write failed" in failed


def test_an_unrecoverable_record_says_to_rerun_the_ingest() -> None:
    record = SpoolRecord(
        kind="ingest-unrecoverable",
        collection="c",
        name="n",
        payload="p",
        recoverable=False,
    )

    assert "re-run the ingest" in _reason(_Spool(), _Unit(record))


def test_only_a_job_whose_record_was_written_is_marked_spooled() -> None:
    record = SpoolRecord(kind="remember", collection="c", name="n", payload="p")

    assert _outcome(_Spool(ok=True), _Unit(record)).spooled
    assert not _outcome(_Spool(ok=False), _Unit(record)).spooled
    assert not _outcome(_Spool(), _Unit(None)).spooled
    assert not _outcome(_Spool(), _Rerunnable()).spooled


def test_a_url_that_cannot_be_rerun_is_spooled_but_not_recoverable() -> None:
    record = SpoolRecord(
        kind="ingest", collection="c", name="n", payload="p", recoverable=False
    )

    outcome = _outcome(_Spool(ok=True), _Unit(record))

    assert (outcome.spooled, outcome.recoverable) == (True, False)
    assert "not recoverable" in outcome.reason


def test_a_recoverable_record_is_spooled_and_recoverable() -> None:
    record = SpoolRecord(kind="remember", collection="c", name="n", payload="p")

    outcome = _outcome(_Spool(ok=True), _Unit(record))

    assert (outcome.spooled, outcome.recoverable) == (True, True)


def test_a_failed_spool_write_is_lost_and_nothing_else_is_claimed() -> None:
    record = SpoolRecord(kind="remember", collection="c", name="n", payload="p")

    outcome = _outcome(_Spool(ok=False), _Unit(record))

    assert (outcome.lost, outcome.spooled, outcome.recoverable) == (True, False, False)


def test_no_other_outcome_is_lost() -> None:
    record = SpoolRecord(kind="remember", collection="c", name="n", payload="p")

    assert not _outcome(_Spool(ok=True), _Unit(record)).lost
    assert not _outcome(_Spool(), _Unit(None)).lost
    assert not _outcome(_Spool(), _Rerunnable()).lost


def test_a_tally_counts_each_aborted_job_exactly_once() -> None:
    tally = AbortTally()
    for outcome in (
        Aborted("a", spooled=True, recoverable=True),
        Aborted("b", spooled=True),
        Aborted("c", lost=True),
        Aborted("d"),
    ):
        tally.record(outcome)

    assert tally.aborted == 4
    assert tally.summary() == (
        "4 job(s): 1 spooled for recovery, 1 spooled but not recoverable, "
        "1 lost (spool write failed), 1 needed no spool"
    )


def test_tallies_combine_into_one_whose_terms_still_add_up() -> None:
    first, second = AbortTally(), AbortTally()
    first.record(Aborted("a", spooled=True, recoverable=True))
    second.record(Aborted("b", lost=True))
    second.record(Aborted("c"))

    total = AbortTally.total([first, second])

    assert total.aborted == 3
    assert "1 spooled for recovery, 0 spooled but not recoverable" in total.summary()
    assert "1 lost (spool write failed), 1 needed no spool" in total.summary()
