"""Store one read transcript: capture file, then the writer, stopping when told to."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.backfill_session import BackfillSession
from quarry.capture import CaptureWriter
from quarry.capture_directory import CaptureDirectory
from quarry.capture_outcome import CaptureOutcome
from quarry.capture_reaper import CaptureReaper
from quarry.redacted_failure import RedactedFailure
from quarry.refusal import RefusalError

if TYPE_CHECKING:
    from quarry.backfill_run import BackfillRun
    from quarry.loaded_transcript import LoadedTranscript
    from quarry.stored_sessions import StoredSessions
    from quarry.transcript_read import TranscriptRead

logger = logging.getLogger(__name__)


@final
class TranscriptStore:
    """Hand one transcript's text to the capture file and then to the writer.

    The stop is checked before each step that can block: the capture file, and
    the hand-off to the writer (whose first step is the scrub).  A transcript
    stopped there is counted as left, not attempted.  Whatever the writer
    answers is counted by what it means; a store that landed is remembered, so
    the next run finds the project's session current.
    """

    __slots__ = ("_collection", "_ingested", "_project_path", "_run", "_stored")

    _collection: str
    _ingested: int
    _project_path: str
    _run: BackfillRun
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
        return self

    @property
    def ingested(self) -> int:
        """Return how many transcripts this store has taken."""
        return self._ingested

    def begin(self) -> None:
        """Reap the temp files crashed capture writes left in the project's captures.

        Once per run for each project, never per write.
        """
        CaptureReaper(CaptureDirectory(Path(self._project_path))).reap()

    def store(self, loaded: LoadedTranscript) -> None:
        """Write the capture file, then file the transcript through the writer."""
        read = loaded.read
        session = BackfillSession(read.transcript, self._project_path, loaded.text)
        if self._stopped() or not self._captured(session, read) or self._stopped():
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

    def _stopped(self) -> bool:
        """Return whether the run was stopped, counting this transcript as left."""
        if not self._run.tally.halted():
            return False
        self._run.tally.leave(1)
        return True

    def _captured(self, session: BackfillSession, read: TranscriptRead) -> bool:
        """Write the capture file; return whether the transcript goes on to the store.

        Nothing to capture is a skip, but not a verdict on the transcript: it is
        not remembered, so a later run looks at it again.  A file that was refused
        or could not be written fails the transcript, with the reason.
        """
        result = CaptureWriter().outcome(session.capture_request())
        match result.outcome:
            case CaptureOutcome.EMPTY:
                self._run.outcomes.nothing_to_capture()
            case CaptureOutcome.FAILED:
                self._run.outcomes.capture_failed(read.transcript, result.cause)
            case CaptureOutcome.SYMLINK:
                reason = result.outcome.value
                logger.warning("backfill: %s: %s", read.transcript.name, reason)
                self._run.tally.fail(f"{read.transcript.name}: {reason}")
            case _:
                return True
        return False
