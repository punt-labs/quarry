"""Reading and archiving a Claude Code session transcript (raw JSONL)."""

from __future__ import annotations

import contextlib
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, final

from quarry.transcript_records import ParsedRecords, TranscriptRecords
from quarry.transcript_turns import TurnText

logger = logging.getLogger(__name__)


@final
class TranscriptReader:
    """Reads a Claude Code JSONL transcript into conversation text and archives it.

    The transcript file is untrusted, possibly-large local input: unparseable
    lines are skipped, an over-budget conversation is front-truncated to keep the
    newest turns, and a read failure degrades to empty rather than raising — a
    capture must never block compaction.  The records kept are bounded to the
    newest :attr:`~quarry.transcript_records.TranscriptRecords.MAX`.
    """

    _MAX_CHARS = 500_000
    _RETENTION_DAYS = 90

    __slots__ = ("_parsed", "_path")

    _parsed: ParsedRecords
    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        self._parsed = TranscriptRecords.parse(cls._read_leniently(path))
        self._parsed.warn_if_truncated(path)
        return self

    @classmethod
    def from_text(cls, path: Path, raw: str) -> Self:
        """Return the reader for *path*'s conversation, given the *raw* text it held.

        The one parse every extraction shares, done once, here: a SubagentStop
        asks one reader for the conversation text and for the final turn, and a
        500 KB transcript must not be parsed again per question.  A caller that
        must tell a read fault from an empty conversation reads the file
        itself, strictly, and hands the text it got to this.
        """
        reader = super().__new__(cls)
        reader._path = path
        reader._parsed = TranscriptRecords.parse(raw)
        reader._parsed.warn_if_truncated(path)
        return reader

    @property
    def truncated(self) -> int:
        """Return how many records or entries the record and character bounds cost."""
        return self._parsed.dropped + self._parsed.fit(self._MAX_CHARS)[1]

    @staticmethod
    def _read_leniently(path: Path) -> str:
        """Return *path*'s text; a missing or unreadable file yields ``""``."""
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning("transcript: could not read %s", path)
            return ""

    def text(self) -> str:
        """Return the transcript's conversation text, newest-first truncated.

        Extracts user and assistant messages, prefixing each with the role, and
        skips tool-use content blocks, file snapshots, and system messages.  A
        missing or unreadable file yields ``""``; a transcript with any message
        never does: the newest entry is always kept, cut to its tail if it alone
        exceeds the budget.
        """
        text, cut = self._parsed.fit(self._MAX_CHARS)
        if cut:
            logger.warning(
                "transcript: %s: dropped or cut %d oldest entries past the "
                "character budget",
                self._path.name,
                cut,
            )
        return text

    def last_assistant_text(self) -> str:
        """Return the final assistant turn's text without its role prefix, or ``""``.

        The last assistant message is the report a subagent hands its parent;
        tool-use blocks are not text, so a turn holding only those is skipped.
        """
        last = ""
        for record in self._parsed.records:
            if record.get("type") == "assistant" and (
                text := TurnText.turn_text(record)
            ):
                last = text
        return last.strip()

    def archive(self, session_id: str, sessions_dir: Path) -> None:
        """Copy the raw JSONL to *sessions_dir*, dedup priors, prune retention.

        Creates the directory if needed, deduplicates prior archives for the same
        session, and lazily prunes files older than ``_RETENTION_DAYS``.
        """
        sessions_dir.mkdir(parents=True, exist_ok=True)

        prefix = f"session-{session_id[:8]}-"

        # Copy first — prior archives survive if this fails.
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        dest = sessions_dir / f"{prefix}{timestamp}.jsonl"
        shutil.copy(self._path, dest)

        # Then dedup: remove prior archives, excluding the one we just wrote.
        for existing in sessions_dir.glob(f"{prefix}*.jsonl"):
            if existing != dest:
                with contextlib.suppress(OSError):
                    existing.unlink()

        # Lazy retention cleanup.
        now = datetime.now(UTC).timestamp()
        retention_seconds = self._RETENTION_DAYS * 86400
        for f in sessions_dir.glob("session-*.jsonl"):
            with contextlib.suppress(OSError):
                if now - f.stat().st_mtime > retention_seconds:
                    f.unlink()
