"""AbortReason: what a drain-aborted job's terminal error says, and what it spools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast, final

from quarry.daemon.abort_reason import Aborted, AbortReason
from quarry.daemon.abort_tally import AbortTally, AbortTerm
from quarry.daemon.job_spool import JobSpool, SpoolRecord

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.tasks import TaskState


class _Spool:
    """A spool double: writes succeed or fail, and every record is kept."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.written: list[SpoolRecord] = []

    def write(self, record: SpoolRecord) -> Path | None:
        self.written.append(record)
        return Path(f"/spool/{record.kind}-x.json") if self.ok else None


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


def test_only_a_job_whose_record_was_written_is_counted_spooled() -> None:
    record = SpoolRecord(kind="remember", collection="c", name="n", payload="p")

    assert _outcome(_Spool(ok=True), _Unit(record)).term is AbortTerm.SPOOLED
    assert _outcome(_Spool(ok=False), _Unit(record)).term is AbortTerm.LOST
    assert _outcome(_Spool(), _Unit(None)).term is AbortTerm.NO_SPOOL
    assert _outcome(_Spool(), _Rerunnable()).term is AbortTerm.NO_SPOOL


def test_the_outcome_names_the_file_it_wrote_so_a_store_can_remove_it() -> None:
    record = SpoolRecord(kind="remember", collection="c", name="n", payload="p")

    assert _outcome(_Spool(ok=True), _Unit(record)).path == Path(
        "/spool/remember-x.json"
    )
    assert _outcome(_Spool(ok=False), _Unit(record)).path is None
    assert _outcome(_Spool(), _Unit(None)).path is None


def test_a_url_that_cannot_be_rerun_is_spooled_but_not_recoverable() -> None:
    record = SpoolRecord(
        kind="ingest", collection="c", name="n", payload="p", recoverable=False
    )

    outcome = _outcome(_Spool(ok=True), _Unit(record))

    assert outcome.term is AbortTerm.UNRECOVERABLE
    assert "not recoverable" in outcome.reason


def _counted(*outcomes: Aborted) -> AbortTally:
    tally = AbortTally()
    for outcome in outcomes:
        job = object()
        tally.take(job)
        tally.move(job, outcome.term)
    return tally


def test_a_tally_counts_each_aborted_job_exactly_once() -> None:
    tally = _counted(
        Aborted("a", AbortTerm.SPOOLED),
        Aborted("b", AbortTerm.UNRECOVERABLE),
        Aborted("c", AbortTerm.LOST),
        Aborted("d"),
        Aborted("e", AbortTerm.KEPT),
        Aborted("f", AbortTerm.UNCONFIRMED),
    )

    assert tally.summary() == (
        "6 job(s): 1 spooled for recovery, 1 spooled but not recoverable, "
        "1 lost (spool write failed), 1 needed no spool, "
        "1 kept (withdrawn, aborted or stored), "
        "1 unconfirmed (committing at shutdown)"
    )


def test_a_job_moves_between_terms_and_is_counted_in_one() -> None:
    tally = AbortTally()
    job = object()

    assert tally.take(job)
    assert not tally.take(job)
    assert tally.pending == 1
    tally.move(job, AbortTerm.UNFINISHED)
    tally.move(job, AbortTerm.SPOOLED)

    assert tally.term_of(job) is AbortTerm.SPOOLED
    assert tally.pending == 0
    assert tally.count(AbortTerm.UNFINISHED) == 0
    assert tally.summary().startswith("1 job(s): 1 spooled for recovery")


def test_a_job_never_taken_has_no_term() -> None:
    assert AbortTally().term_of(object()) is None


def test_a_pending_job_is_not_in_the_summary_until_its_outcome_is_known() -> None:
    tally = AbortTally()
    tally.take(object())

    assert tally.pending == 1
    assert tally.summary().startswith("0 job(s)")


def test_tallies_combine_into_one_whose_terms_still_add_up() -> None:
    first = _counted(Aborted("a", AbortTerm.SPOOLED))
    second = _counted(Aborted("b", AbortTerm.LOST), Aborted("c"))

    total = AbortTally.total([first, second])

    assert total.summary().startswith("3 job(s)")
    assert "1 spooled for recovery, 0 spooled but not recoverable" in total.summary()
    assert "1 lost (spool write failed), 1 needed no spool" in total.summary()
