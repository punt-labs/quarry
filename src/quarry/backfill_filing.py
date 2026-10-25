"""File one project's transcripts one at a time: classify, read, then store."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.backfill_store import TranscriptStore
from quarry.transcript_loader import TranscriptLoader
from quarry.transcript_refusals import UnreadTranscriptError
from quarry.unread_transcripts import UnreadTranscripts

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.backfill_run import BackfillRun
    from quarry.stored_sessions import StoredSessions


@final
class TranscriptFiler:
    """File one project's transcripts, each ending in exactly one outcome.

    A transcript is already current, known unindexable, unreadable, empty,
    attempted (stored or failed), or not attempted because the run was stopped.
    The stop is checked before every step that can block (here the read; the
    capture file and the writer in :class:`~quarry.backfill_store.TranscriptStore`).
    Only an attempt consumes ``--limit``: a transcript that could not be read, or
    was refused for what it is, is counted failed but never attempted.
    """

    __slots__ = ("_run", "_skipped", "_store", "_stored")

    _run: BackfillRun
    _skipped: int
    _store: TranscriptStore
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
        self._stored = stored
        self._store = TranscriptStore(run, project_path, collection, stored)
        self._skipped = 0
        return self

    @property
    def ingested(self) -> int:
        """Return how many transcripts this filer stored."""
        return self._store.ingested

    @property
    def skipped(self) -> int:
        """Return how many transcripts it found already current."""
        return self._skipped

    def file_all(self, transcripts: list[Path]) -> None:
        """File *transcripts* in order, until done, limited or halted.

        A halt counts the ones still needing work as not attempted.
        """
        tally = self._run.tally
        self._begin()
        for index, transcript in enumerate(transcripts):
            if self._run.config.reached_by(tally.processed):
                break
            if tally.halted():
                tally.leave(self._pending(transcripts[index:]))
                break
            self.file(transcript)

    def _begin(self) -> None:
        """Start the project: its captures directory is swept of crashed leftovers.

        A dry run changes nothing, so it sweeps nothing.
        """
        if not self._run.config.dry_run:
            self._store.begin()

    def file(self, transcript: Path) -> None:
        """Classify one transcript and file it if it has anything to file."""
        if self._settle(transcript):
            return
        try:
            loaded = TranscriptLoader.load(transcript)
        except (OSError, UnicodeDecodeError, UnreadTranscriptError) as exc:
            UnreadTranscripts(self._run).counted(transcript, exc)
            return
        if loaded.truncated:
            self._run.tally.truncated()
        if not loaded.text.strip():
            if loaded.truncated:
                self._run.outcomes.tail_without_text(transcript)
            else:
                self._run.outcomes.unindexable(loaded.read)
        elif self._run.config.dry_run:
            self._run.outcomes.would_store()
        else:
            self._store.store(loaded)

    def _pending(self, transcripts: list[Path]) -> int:
        """Return how many of *transcripts* still need work."""
        return sum(1 for t in transcripts if not self._settled_by(t))

    def _settle(self, transcript: Path) -> bool:
        """Count and return whether *transcript* needs no work."""
        match self._settled_by(transcript):
            case "current":
                self._skipped += 1
                self._run.tally.skip_existing()
            case "unindexable":
                self._run.tally.skip_empty()
            case _:
                return False
        return True

    def _settled_by(self, transcript: Path) -> str:
        """Return why *transcript* needs no work, or ``""`` if it does."""
        if self._stored.is_current(transcript):
            return "current"
        if self._run.unindexable.knows(transcript):
            return "unindexable"
        return ""
