"""Reading and archiving a Claude Code session transcript (raw JSONL)."""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, final

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
    _MAX_TOOL_RESULT_CHARS = 500
    _RETENTION_DAYS = 90

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    def text(self) -> str:
        """Return the transcript's conversation text, newest-first truncated.

        Extracts user and assistant messages, prefixing each with the role, and
        skips tool-use content blocks, file snapshots, and system messages.  A
        missing or unreadable file yields ``""``.
        """
        if not self._path.is_file():
            return ""
        try:
            raw = self._path.read_text()
        except (OSError, UnicodeDecodeError):
            logger.warning("pre-compact: could not read transcript %s", self._path)
            return ""
        parts: list[str] = []
        for line in raw.splitlines():
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            entry = self.message_text(obj)
            if entry:
                parts.append(entry)
        return self._join_within_budget(parts)

    def _join_within_budget(self, parts: list[str]) -> str:
        """Drop the oldest entries until the joined text fits the char budget."""
        total_chars = sum(len(p) for p in parts)
        start = 0
        while start < len(parts) and total_chars > self._MAX_CHARS:
            total_chars -= len(parts[start])
            start += 1
        if start > 0:
            logger.debug(
                "pre-compact: dropped %d oldest entries from transcript",
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

    @classmethod
    def message_text(cls, record: dict[str, object]) -> str | None:
        """Return a single record's ``[role] text``, or ``None`` if not a message.

        ``None`` is the documented "this record is not a user/assistant message"
        contract (a file snapshot or system record), not a failure — the caller
        skips it.
        """
        record_type = record.get("type", "")
        if record_type not in ("user", "assistant"):
            return None
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role", record_type)
        content = message.get("content")
        if isinstance(content, str):
            return f"[{role}] {content}" if content.strip() else None
        if not isinstance(content, list):
            return None
        texts = cls._content_texts(content)
        if not texts:
            return None
        return f"[{role}] {' '.join(texts)}"

    @classmethod
    def _content_texts(cls, content: list[object]) -> list[str]:
        """Extract text fragments from a list of content blocks."""
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                if tool_text := cls._tool_result_text(block):
                    texts.append(f"[tool_result] {tool_text}")
            elif stripped := cls._block_text(block):
                texts.append(stripped)
        return texts

    @classmethod
    def _tool_result_text(cls, block: dict[str, object]) -> str:
        """Return the tool_result text if under the per-result cap, else ``""``."""
        tool_content = block.get("content")
        if isinstance(tool_content, str):
            tool_text = tool_content.strip()
        elif isinstance(tool_content, list):
            tool_text = " ".join(
                t
                for b in tool_content
                if isinstance(b, dict) and (t := cls._block_text(b))
            )
        else:
            tool_text = ""
        if 0 < len(tool_text) <= cls._MAX_TOOL_RESULT_CHARS:
            return tool_text
        return ""

    @staticmethod
    def _block_text(block: dict[str, object]) -> str:
        """Return a text block's stripped text, or ``""`` if not a text block."""
        if block.get("type") != "text":
            return ""
        text = block.get("text")
        return text.strip() if isinstance(text, str) else ""
