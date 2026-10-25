"""File one project's transcripts, classifying each and tallying the run."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, final

from quarry.backfill_session import BackfillSession
from quarry.backfill_writer import BackfillWriteError, WriteOutcome
from quarry.failure_reason import FailureReason
from quarry.redacted_failure import RedactedFailure
from quarry.refusal import RefusalError
from quarry.stored_sessions import StoredSessions
from quarry.transcript_reader import TranscriptReader

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.backfill import BackfillConfig, BackfillRuntime
    from quarry.backfill_mapping import ProjectMapping
    from quarry.backfill_stats import BackfillTally
    from quarry.backfill_unindexable import UnindexableTranscripts

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class BackfillRun:
    """The parts of one run every project processor shares."""

    runtime: BackfillRuntime
    config: BackfillConfig
    tally: BackfillTally
    unindexable: UnindexableTranscripts

    def process(self, mappings: list[ProjectMapping]) -> None:
        """File every project in *mappings* in turn, until done, limited or halted."""
        for mapping in mappings:
            if self.config.reached_by(self.tally.processed):
                limit = self.config.limit
                logger.info("backfill: reached limit of %d transcripts", limit)
                break
            if self.tally.halted():
                self.tally.not_attempted += len(mapping.transcript_files())
                continue
            ProjectProcessor(mapping, self).process()


@final
class ProjectProcessor:
    """File one project's uncaptured or grown transcripts, tallying the run.

    Every transcript ends in exactly one outcome: already current, known
    unindexable, unreadable, empty, attempted (stored or failed), or not
    attempted because the run was stopped.  Only an attempt consumes ``--limit``.
    """

    _mapping: ProjectMapping
    _run: BackfillRun
    _collection: str
    _stored: StoredSessions
    _ingested: int
    _skipped: int

    def __new__(cls, mapping: ProjectMapping, run: BackfillRun) -> Self:
        self = super().__new__(cls)
        self._mapping = mapping
        self._run = run
        self._collection = run.config.collection_override or mapping.captures_collection
        self._stored = StoredSessions([])
        self._ingested = 0
        self._skipped = 0
        return self

    def process(self) -> None:
        """File every transcript for this project, then log the tally."""
        transcripts = self._mapping.transcript_files()
        if not transcripts:
            return
        tally = self._run.tally
        try:
            docs = self._run.runtime.database.catalog.list_documents(
                collection_filter=self._collection
            )
        except Exception as exc:
            tally.errors.append(
                f"{self._mapping.project_path}: {FailureReason.of(exc)}"
            )
            tally.not_attempted += len(transcripts)
            logger.error(
                "backfill: could not read the store for %s: %s",
                self._mapping.project_path,
                type(exc).__name__,
                exc_info=RedactedFailure.of(exc),
            )
            return
        self._stored = StoredSessions(docs)
        for index, transcript in enumerate(transcripts):
            if self._run.config.reached_by(tally.processed):
                break
            if tally.halted():
                tally.not_attempted += len(transcripts) - index
                break
            self._handle(transcript)
        logger.info(
            "backfill: %s -> %s (%d new, %d already ingested)",
            self._mapping.project_path,
            self._collection,
            self._ingested,
            self._skipped,
        )

    def _handle(self, transcript: Path) -> None:
        """Classify one transcript and file it if it has anything to file."""
        tally = self._run.tally
        if self._stored.is_current(transcript):
            self._skipped += 1
            tally.skipped_existing += 1
            return
        if self._run.unindexable.knows(transcript):
            tally.skipped_empty += 1
            return
        # Taken before the read: a write after this instant is newer than what is
        # stored, whatever happens between the read and the store.
        snapshot = datetime.now(UTC)
        if not (transcript.is_file() and os.access(transcript, os.R_OK)):
            tally.skipped_unreadable += 1
            logger.warning(
                "backfill: %s is not a readable file; skipped", transcript.name
            )
            return
        text = TranscriptReader(transcript).text()
        if not text.strip():
            self._skip_unindexable(transcript)
        elif self._run.config.dry_run:
            tally.would_ingest += 1
            tally.processed += 1
        else:
            self._ingest(transcript, text, snapshot)

    def _skip_unindexable(self, transcript: Path) -> None:
        """Count a transcript with nothing to index, and remember it for later runs."""
        self._run.tally.skipped_empty += 1
        if not self._run.config.dry_run:
            self._run.unindexable.record(transcript)

    def _ingest(self, transcript: Path, text: str, snapshot: datetime) -> None:
        """Write the capture file, then file the transcript through the writer."""
        tally = self._run.tally
        session = BackfillSession(transcript, self._mapping.project_path, text)
        try:
            if not session.write_capture_file():
                msg = "capture file could not be written"
                raise BackfillWriteError(msg)
            self._run.runtime.writer.write(self._collection, session, snapshot)
        except RefusalError as exc:
            self._not_stored(transcript, exc)
        except Exception as exc:
            logger.error(
                "backfill: %s failed: %s",
                transcript.name,
                type(exc).__name__,
                exc_info=RedactedFailure.of(exc),
            )
            self._failed(transcript, exc)
        else:
            self._stored.record(transcript, snapshot)
            self._ingested += 1
            tally.ingested += 1
            tally.processed += 1
            tally.admitted()

    def _not_stored(self, transcript: Path, exc: RefusalError) -> None:
        """Count a transcript refused, by what the run should make of the refusal."""
        tally = self._run.tally
        outcome = exc.outcome if isinstance(exc, BackfillWriteError) else None
        if outcome is WriteOutcome.ABORTED:
            tally.abort(str(exc))
            tally.not_attempted += 1
        elif outcome is WriteOutcome.UNINDEXABLE:
            self._skip_unindexable(transcript)
        else:
            logger.warning("backfill: %s not stored: %s", transcript.name, exc)
            self._failed(
                transcript, exc, queue_full=outcome is WriteOutcome.NOT_ADMITTED
            )

    def _failed(
        self, transcript: Path, exc: Exception, *, queue_full: bool = False
    ) -> None:
        """Count an attempted transcript that was not stored."""
        tally = self._run.tally
        tally.errors.append(FailureReason.line(transcript.name, exc))
        tally.failed += 1
        tally.processed += 1
        if queue_full:
            tally.not_admitted()
        else:
            tally.admitted()
