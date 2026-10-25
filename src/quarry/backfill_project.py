"""File each registered project's transcripts, tallying the run."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.backfill_filing import TranscriptFiler
from quarry.failure_reason import FailureReason
from quarry.redacted_failure import RedactedFailure
from quarry.stored_sessions import StoredSessions

if TYPE_CHECKING:
    from quarry.backfill_mapping import ProjectMapping
    from quarry.backfill_run import BackfillRun

logger = logging.getLogger(__name__)


@final
class ProjectProcessor:
    """File one project's uncaptured or grown transcripts, tallying the run."""

    __slots__ = ("_collection", "_mapping", "_run")

    _collection: str
    _mapping: ProjectMapping
    _run: BackfillRun

    def __new__(cls, mapping: ProjectMapping, run: BackfillRun) -> Self:
        self = super().__new__(cls)
        self._mapping = mapping
        self._run = run
        self._collection = run.config.collection_override or mapping.captures_collection
        return self

    def process(self) -> None:
        """File every transcript for this project, then log the tally."""
        transcripts = self._mapping.transcript_files()
        if not transcripts:
            return
        tally = self._run.tally
        if tally.halted():  # before the store is read
            tally.leave_unexamined(len(transcripts))
            return
        stored = self._stored_sessions(len(transcripts))
        if stored is None:
            return
        filer = TranscriptFiler(
            self._run, self._mapping.project_path, self._collection, stored
        )
        filer.file_all(transcripts)
        logger.info(
            "backfill: %s -> %s (%d new, %d already ingested)",
            self._mapping.project_path,
            self._collection,
            filer.ingested,
            filer.skipped,
        )

    def _stored_sessions(self, count: int) -> StoredSessions | None:
        """Return what the store holds for this project, or ``None`` if unreadable.

        ``None`` is the documented contract: the store could not be listed, the
        failure is counted and logged, and the project's *count* transcripts are
        not examined.
        """
        try:
            docs = self._run.runtime.database.catalog.list_documents(
                collection_filter=self._collection
            )
        except Exception as exc:
            self._run.tally.note(
                f"{self._mapping.project_path}: {FailureReason.of(exc)}"
            )
            self._run.tally.leave_unexamined(count)
            logger.error(
                "backfill: could not read the store for %s: %s",
                self._mapping.project_path,
                type(exc).__name__,
                exc_info=RedactedFailure.of(exc),
            )
            return None
        return StoredSessions(docs)
