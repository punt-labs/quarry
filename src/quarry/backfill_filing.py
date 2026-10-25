"""File one project's transcripts one at a time: classify, read, then store."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.backfill_session import BackfillSession
from quarry.read_fault import ReadFault
from quarry.redacted_failure import RedactedFailure
from quarry.refusal import RefusalError
from quarry.transcript_loader import TranscriptLoader, TranscriptTooLargeError

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.backfill_run import BackfillRun
    from quarry.stored_sessions import StoredSessions
    from quarry.transcript_loader import LoadedTranscript

logger = logging.getLogger(__name__)


@final
class TranscriptFiler:
    """File one project's transcripts, each ending in exactly one outcome.

    A transcript is already current, known unindexable, unreadable, empty,
    attempted (stored or failed), or not attempted because the run was stopped.
    The stop is checked before every step that can block: the read, the capture
    file, and the hand-off to the writer (whose first step is the scrub).  Only
    an attempt consumes ``--limit``.
    """

    __slots__ = (
        "_collection",
        "_ingested",
        "_project_path",
        "_run",
        "_skipped",
        "_stored",
    )

    _collection: str
    _ingested: int
    _project_path: str
    _run: BackfillRun
    _skipped: int
    _stored: StoredSessions

    def __new__(
        cls,
        run: BackfillRun,
        project_path: str,
        collection: str,
        stored: StoredSessions,
    ) -> Self:
        self = super().__new__(cls)
        self._run = run
        self._project_path = project_path
        self._collection = collection
        self._stored = stored
        self._ingested = 0
        self._skipped = 0
        return self

    @property
    def ingested(self) -> int:
        """Return how many transcripts this filer stored."""
        return self._ingested

    @property
    def skipped(self) -> int:
        """Return how many transcripts it found already current."""
        return self._skipped

    def file_all(self, transcripts: list[Path]) -> None:
        """File *transcripts* in order, until done, limited or halted.

        A halt counts the ones still needing work as not attempted.
        """
        tally = self._run.tally
        for index, transcript in enumerate(transcripts):
            if self._run.config.reached_by(tally.processed):
                break
            if tally.halted():
                tally.leave(self._pending(transcripts[index:]))
                break
            self.file(transcript)

    def _pending(self, transcripts: list[Path]) -> int:
        """Return how many of *transcripts* still need work."""
        return sum(1 for t in transcripts if not self._is_settled(t))

    def file(self, transcript: Path) -> None:
        """Classify one transcript and file it if it has anything to file."""
        if self._settle(transcript):
            return
        try:
            loaded = TranscriptLoader.load(transcript)
        except (OSError, UnicodeDecodeError, TranscriptTooLargeError) as exc:
            self._unreadable(transcript, ReadFault(exc))
            return
        if not loaded.text.strip():
            self._run.outcomes.unindexable(loaded.read)
        elif self._run.config.dry_run:
            self._run.outcomes.would_store()
        else:
            self._ingest(loaded)

    def _unreadable(self, transcript: Path, fault: ReadFault) -> None:
        """Count a transcript that could not be read: a skip if it vanished."""
        if fault.vanished:
            logger.info("backfill: %s vanished; skipped", transcript.name)
            self._run.tally.skip_unreadable()
            return
        logger.warning("backfill: %s not read: %s", transcript.name, fault.reason)
        self._run.tally.fail(f"{transcript.name}: {fault.reason}")

    def _is_settled(self, transcript: Path) -> bool:
        """Return whether *transcript* needs no work (current, or known empty)."""
        return self._stored.is_current(transcript) or self._run.unindexable.knows(
            transcript
        )

    def _settle(self, transcript: Path) -> bool:
        """Count and return whether *transcript* needs no work."""
        tally = self._run.tally
        if self._stored.is_current(transcript):
            self._skipped += 1
            tally.skip_existing()
            return True
        if self._run.unindexable.knows(transcript):
            tally.skip_empty()
            return True
        return False

    def _stopped(self) -> bool:
        """Return whether the run was stopped, counting this transcript as left."""
        if not self._run.tally.halted():
            return False
        self._run.tally.leave(1)
        return True

    def _ingest(self, loaded: LoadedTranscript) -> None:
        """Write the capture file, then file the transcript through the writer."""
        read = loaded.read
        session = BackfillSession(read.transcript, self._project_path, loaded.text)
        if self._stopped():
            return
        if not session.write_capture_file():
            self._run.outcomes.capture_failed(read.transcript)
            return
        if self._stopped():
            return
        try:
            self._run.runtime.writer.write(self._collection, session, read.snapshot)
        except RefusalError as exc:
            self._run.outcomes.refused(read, exc)
        except Exception as exc:
            logger.error(
                "backfill: %s failed: %s",
                read.transcript.name,
                type(exc).__name__,
                exc_info=RedactedFailure.of(exc),
            )
            self._run.outcomes.failed(read.transcript, exc)
        else:
            self._stored.record(read.transcript, read.snapshot)
            self._ingested += 1
            self._run.outcomes.stored()
