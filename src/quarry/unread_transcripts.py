"""Count a transcript that could not be read, by why."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.read_fault import ReadFault
from quarry.transcript_refusals import UnreadTranscriptError

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.backfill_run import BackfillRun

logger = logging.getLogger(__name__)


@final
class UnreadTranscripts:
    """The tally's account of a transcript that was not read.

    A file that vanished after it was listed is a skip.  One refused for what it
    is (too large, not a file) is a refusal: counted as refused, never as empty
    or failed, and noted on every run, because it is re-detected from the file's
    own stat (its size, its type) rather than remembered.  Any other fault is a
    failure that never counts as an attempt, so it does not use up ``--limit``.
    """

    __slots__ = ("_run",)

    _run: BackfillRun

    def __new__(cls, run: BackfillRun) -> Self:
        self = super().__new__(cls)
        self._run = run
        return self

    def counted(self, transcript: Path, exc: Exception) -> None:
        """Count *transcript*, which failed to read with *exc*."""
        fault = ReadFault(exc)
        if fault.vanished:
            logger.info("backfill: %s vanished; skipped", transcript.name)
            self._run.tally.skip_unreadable()
            return
        logger.warning("backfill: %s not read: %s", transcript.name, fault.reason)
        line = f"{transcript.name}: {fault.reason}"
        if isinstance(exc, UnreadTranscriptError):
            self._run.tally.skip_refused(line)
        else:
            self._run.tally.fail_unread(line)
