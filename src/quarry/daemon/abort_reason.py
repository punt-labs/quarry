"""The truthful terminal error for a job the shutdown drain did not run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self, final

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

    UNFINISHED: ClassVar[Aborted] = Aborted(
        "daemon shut down before ingest; spool unfinished at shutdown, "
        "content may or may not be saved",
        AbortTerm.UNFINISHED,
    )
    UNBUILT: ClassVar[Aborted] = Aborted(
        "daemon shut down before ingest; spool record could not be built, "
        "content not recovered",
        AbortTerm.UNBUILT,
    )
    NOT_WRITTEN: ClassVar[Aborted] = Aborted(
        "daemon shut down before ingest; spool write failed, content not recovered",
        AbortTerm.NOT_WRITTEN,
    )

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

    @staticmethod
    def without_spool(job: IngestUnit) -> Aborted:
        """Return the outcome of *job* when nothing is spooled for it.

        A job whose source outlives it says how to run it again; any other has a
        durable copy elsewhere, or is a committing job the abort left to its store.
        """
        if isinstance(job, RerunnableUnit):
            return Aborted(
                f"daemon shut down before ingest completed; {job.rerun_hint}"
            )
        return Aborted("daemon shut down before ingest completed")

    @staticmethod
    def is_durable(job: IngestUnit) -> bool:
        """Return whether something outside the daemon holds *job*'s content.

        A job whose source outlives it is run again from there, and one that has
        no spool record has a durable client copy; any other would be lost if it
        were not spooled.
        """
        return isinstance(job, RerunnableUnit) or job.spool_record() is None

    def of(self, job: IngestUnit) -> Aborted:
        """Return the outcome for *job*, spooling it first if it needs it."""
        if isinstance(job, RerunnableUnit) or (record := job.spool_record()) is None:
            return self.without_spool(job)
        if (path := self._spool.write(record)) is None:
            return self.NOT_WRITTEN
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
