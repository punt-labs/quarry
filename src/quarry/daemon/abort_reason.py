"""The truthful terminal error for a job the shutdown drain did not run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.abort_tally import AbortTerm
from quarry.daemon.job_spool import JobSpool
from quarry.daemon.rerunnable_unit import RerunnableUnit

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.config import Settings
    from quarry.daemon.ingest_unit import IngestUnit


@final
@dataclass(frozen=True, slots=True)
class Aborted:
    """How a drain-aborted job ended: the words, the term it is counted in, its file.

    ``path`` is the spool record the abort wrote, so a job that then stores can
    have it removed; it is ``None`` when none was written.
    """

    reason: str
    term: AbortTerm = AbortTerm.NO_SPOOL
    path: Path | None = None


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
        if (path := self._spool.write(record)) is None:
            return Aborted(
                "daemon shut down before ingest; spool write failed, "
                "content not recovered",
                AbortTerm.LOST,
            )
        if record.recoverable:
            return Aborted(
                "daemon shut down before ingest; spooled for recovery",
                AbortTerm.SPOOLED,
                path,
            )
        return Aborted(
            "daemon shut down before ingest; URL not recoverable "
            "(query or parameters not persisted), re-run the ingest",
            AbortTerm.UNRECOVERABLE,
            path,
        )
