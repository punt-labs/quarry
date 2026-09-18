"""Reading and archiving a Claude Code session transcript (raw JSONL)."""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.transcript_turns import TurnText

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


@final
class TranscriptReader:
    """Reads a Claude Code JSONL transcript into conversation text and archives it.

    The transcript file is untrusted, possibly-large local input: unparseable
    lines are skipped, an over-budget conversation is front-truncated to keep the
    newest turns, and a read failure degrades to empty rather than raising — a
    capture must never block compaction.
    """

    _MAX_CHARS = 500_000
    _RETENTION_DAYS = 90

    __slots__ = ("_path", "_records")

    _path: Path
    _records: tuple[dict[str, object], ...]

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        # Read once, here: a SubagentStop asks one reader for the conversation
        # text and for the final turn, and a 500 KB transcript must not be
        # parsed again per question.
        self._records = tuple(cls._parse(path))
        return self

    @staticmethod
    def _parse(path: Path) -> Iterator[dict[str, object]]:
        """Yield each parsed JSON object in *path*, in file order.

        The one read every extraction shares: a missing or unreadable file
        yields nothing, and a line that is not a JSON object is skipped, so
        no extraction ever sees a partial or malformed record.
        """
        if not path.is_file():
            return
        try:
            raw = path.read_text()
        except (OSError, UnicodeDecodeError):
            logger.warning("transcript: could not read %s", path)
            return
        for line in raw.splitlines():
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                yield obj

    def text(self) -> str:
        """Return the transcript's conversation text, newest-first truncated.

        Extracts user and assistant messages, prefixing each with the role, and
        skips tool-use content blocks, file snapshots, and system messages.  A
        missing or unreadable file yields ``""``.
        """
        parts = [
            entry
            for record in self._records
            if (entry := TurnText.message_text(record))
        ]
        return self._join_within_budget(parts)

    def last_assistant_text(self) -> str:
        """Return the final assistant turn's text without its role prefix, or ``""``.

        The last assistant message is the report a subagent hands its parent;
        tool-use blocks are not text, so a turn holding only those is skipped.
        """
        last = ""
        for record in self._records:
            if record.get("type") == "assistant" and (
                text := TurnText.turn_text(record)
            ):
                last = text
        return last.strip()

    @classmethod
    def _join_within_budget(cls, parts: list[str]) -> str:
        """Drop the oldest entries until the joined text fits the char budget."""
        total_chars = sum(len(p) for p in parts)
        start = 0
        while start < len(parts) and total_chars > cls._MAX_CHARS:
            total_chars -= len(parts[start])
            start += 1
        if start > 0:
            logger.debug(
                "transcript: dropped %d oldest entries from transcript",
                start,
            )
            parts = parts[start:]
        return "\n\n".join(parts)

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
