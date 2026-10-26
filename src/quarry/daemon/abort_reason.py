"""The truthful terminal error for a job the shutdown drain did not run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.job_spool import JobSpool
from quarry.daemon.rerunnable_unit import RerunnableUnit

if TYPE_CHECKING:
    from collections.abc import Iterable

    from quarry.config import Settings
    from quarry.daemon.ingest_unit import IngestUnit


@final
@dataclass(frozen=True, slots=True)
class Aborted:
    """How a drain-aborted job ended: the words, and what became of its spool.

    ``spooled`` says a record was written; ``recoverable`` says it can be run
    again from that record (a URL without its query cannot).  ``lost`` says the
    spool write failed, so the job's content is gone.  ``unfinished`` says the
    write was still running when the abort stopped waiting: the content may or may
    not be on disk.
    """

    reason: str
    spooled: bool = False
    recoverable: bool = False
    lost: bool = False
    unfinished: bool = False


@final
class AbortReason:
    """Say what happened to a drain-aborted job, and how it is recovered.

    A job whose source outlives it (:class:`RerunnableUnit`) is recovered by
    running it again.  One with a durable client copy (a capture's transcript
    ``.md``, recoverable via ``quarry backfill``) returns no spool record.  One
    without is spooled, and ONLY a spool write that actually succeeded may claim
    recoverability, so a failed write (already logged) records the loss
    truthfully rather than a false "spooled" claim that would hide the exact
    silent loss this path prevents.
    """

    __slots__ = ("_spool",)

    _spool: JobSpool

    def __new__(cls, spool: JobSpool) -> Self:
        self = super().__new__(cls)
        self._spool = spool
        return self

    @classmethod
    def for_settings(cls, settings: Settings) -> Self:
        """Return the reasons for a daemon with *settings*."""
        return cls(JobSpool.for_settings(settings))

    def of(self, job: IngestUnit) -> Aborted:
        """Return the outcome for *job*, spooling it first if it needs it."""
        if isinstance(job, RerunnableUnit):
            return Aborted(
                f"daemon shut down before ingest completed; {job.rerun_hint}"
            )
        record = job.spool_record()
        if record is None:
            return Aborted("daemon shut down before ingest completed")
        if not self._spool.write(record):
            return Aborted(
                "daemon shut down before ingest; spool write failed, "
                "content not recovered",
                lost=True,
            )
        if record.recoverable:
            return Aborted(
                "daemon shut down before ingest; spooled for recovery",
                spooled=True,
                recoverable=True,
            )
        return Aborted(
            "daemon shut down before ingest; URL not recoverable "
            "(query or parameters not persisted), re-run the ingest",
            spooled=True,
        )


@final
class AbortTally:
    """Count how drain-aborted jobs ended, so the terms of a summary add up.

    Every aborted job is exactly one of: spooled and recoverable, spooled but not
    recoverable, lost (its spool write failed), unfinished (its spool write was
    still running when the abort stopped waiting), or in need of no spool (it can
    be run again from its source, or a durable copy exists elsewhere).
    """

    __slots__ = ("_aborted", "_lost", "_spooled", "_unfinished", "_unrecoverable")

    _aborted: int
    _lost: int
    _spooled: int
    _unfinished: int
    _unrecoverable: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._aborted = self._lost = self._spooled = 0
        self._unfinished = self._unrecoverable = 0
        return self

    @classmethod
    def total(cls, tallies: Iterable[AbortTally]) -> Self:
        """Return one tally that counts everything *tallies* counted."""
        combined = cls()
        for tally in tallies:
            combined._aborted += tally._aborted
            combined._lost += tally._lost
            combined._spooled += tally._spooled
            combined._unfinished += tally._unfinished
            combined._unrecoverable += tally._unrecoverable
        return combined

    def record(self, outcome: Aborted) -> None:
        """Count one aborted job by what became of its spool."""
        self._aborted += 1
        self._count(outcome, 1)

    def replace(self, old: Aborted, new: Aborted) -> None:
        """Move one counted job from the *old* term to the *new* one."""
        self._count(old, -1)
        self._count(new, 1)

    def _count(self, outcome: Aborted, sign: int) -> None:
        """Add (or, with a negative *sign*, take away) *outcome*'s term."""
        self._lost += sign * outcome.lost
        self._unfinished += sign * outcome.unfinished
        self._spooled += sign * (outcome.spooled and outcome.recoverable)
        self._unrecoverable += sign * (outcome.spooled and not outcome.recoverable)

    def summary(self) -> str:
        """Return the counts as words; the terms sum to the job count."""
        unspooled = (
            self._aborted
            - self._spooled
            - self._unrecoverable
            - self._lost
            - self._unfinished
        )
        unfinished = (
            f"{self._unfinished} spool unfinished at shutdown, "
            if self._unfinished
            else ""
        )
        return (
            f"{self._aborted} job(s): {self._spooled} spooled for recovery, "
            f"{self._unrecoverable} spooled but not recoverable, "
            f"{self._lost} lost (spool write failed), {unfinished}"
            f"{unspooled} needed no spool"
        )
