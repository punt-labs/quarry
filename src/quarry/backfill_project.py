"""File one project's transcripts, classifying each and tallying the run."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, final

from quarry.backfill_outcomes import TranscriptOutcomes
from quarry.backfill_session import BackfillSession
from quarry.failure_reason import FailureReason
from quarry.os_fault import OsFault
from quarry.redacted_failure import RedactedFailure
from quarry.refusal import RefusalError
from quarry.stored_sessions import StoredSessions
from quarry.strict_transcript_reader import StrictTranscriptReader
from quarry.transcript_read import TranscriptRead

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
                self.tally.not_examined += len(mapping.transcript_files())
                continue
            ProjectProcessor(mapping, self).process()

    def close(self) -> None:
        """Save what the run learned; a save that fails is a note, never a fault."""
        unsaved = self.unindexable.save()  # never raises, so it cannot mask a fault
        if unsaved:
            self.tally.errors.append(f"unindexable cache not saved: {unsaved}")


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
    _outcomes: TranscriptOutcomes
    _stored: StoredSessions
    _ingested: int
    _skipped: int

    def __new__(cls, mapping: ProjectMapping, run: BackfillRun) -> Self:
        self = super().__new__(cls)
        self._mapping = mapping
        self._run = run
        self._collection = run.config.collection_override or mapping.captures_collection
        self._outcomes = TranscriptOutcomes(
            run.tally, run.unindexable, dry_run=run.config.dry_run
        )
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
            tally.not_examined += len(transcripts)
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
                tally.not_attempted += self._pending(transcripts[index:])
                break
            self._handle(transcript)
        logger.info(
            "backfill: %s -> %s (%d new, %d already ingested)",
            self._mapping.project_path,
            self._collection,
            self._ingested,
            self._skipped,
        )

    def _pending(self, transcripts: list[Path]) -> int:
        """Return how many of *transcripts* still need work."""
        return sum(
            1
            for transcript in transcripts
            if not (
                self._stored.is_current(transcript)
                or self._run.unindexable.knows(transcript)
            )
        )

    def _handle(self, transcript: Path) -> None:
        """Classify one transcript and file it if it has anything to file."""
        if self._settled(transcript):
            return
        try:
            read, text = self._read(transcript)
        except (OSError, UnicodeDecodeError) as exc:
            self._run.tally.skipped_unreadable += 1
            logger.warning(
                "backfill: %s unreadable (%s); skipped",
                transcript.name,
                OsFault.named(exc),
            )
            return
        if not text.strip():
            self._outcomes.unindexable(read)
        elif self._run.config.dry_run:
            self._outcomes.would_store()
        else:
            self._ingest(read, text)

    def _settled(self, transcript: Path) -> bool:
        """Count and return whether *transcript* needs no work (current, or empty)."""
        tally = self._run.tally
        if self._stored.is_current(transcript):
            self._skipped += 1
            tally.skipped_existing += 1
            return True
        if self._run.unindexable.knows(transcript):
            tally.skipped_empty += 1
            return True
        return False

    @staticmethod
    def _read(transcript: Path) -> tuple[TranscriptRead, str]:
        """Return the transcript as read (snapshot, mtime, text), or raise."""
        snapshot = datetime.now(UTC)
        if not transcript.is_file():
            raise FileNotFoundError(transcript.name)
        mtime_ns = transcript.stat().st_mtime_ns
        text = StrictTranscriptReader(transcript).text()
        return TranscriptRead(transcript, mtime_ns, snapshot), text

    def _ingest(self, read: TranscriptRead, text: str) -> None:
        """Write the capture file, then file the transcript through the writer."""
        session = BackfillSession(read.transcript, self._mapping.project_path, text)
        if not session.write_capture_file():
            self._outcomes.capture_failed(read.transcript)
            return
        try:
            self._run.runtime.writer.write(self._collection, session, read.snapshot)
        except RefusalError as exc:
            self._outcomes.refused(read, exc)
        except Exception as exc:
            logger.error(
                "backfill: %s failed: %s",
                read.transcript.name,
                type(exc).__name__,
                exc_info=RedactedFailure.of(exc),
            )
            self._outcomes.failed(read.transcript, exc)
        else:
            self._stored.record(read.transcript, read.snapshot)
            self._ingested += 1
            self._outcomes.stored()
