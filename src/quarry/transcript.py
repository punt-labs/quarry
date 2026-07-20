"""The Claude Code session transcript file and its derived identifiers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Transcript:
    """A Claude Code session transcript file and its derived identifiers.

    Bundles the several identifiers that were previously computed by free
    functions operating on the same ``Path`` — the session prefix, the mtime
    timestamp, and the stored document name.
    """

    path: Path

    @property
    def session_prefix(self) -> str:
        """Return the first 8 characters of the session id (the file stem)."""
        return self.path.stem[:8]

    def timestamp(self, fmt: str) -> str:
        """Format the file's mtime per *fmt*, degrading to now() if it vanished.

        A transcript deleted or moved mid-run makes ``stat()`` raise
        ``OSError``; falling back to the current time keeps one missing file
        from aborting the whole run.
        """
        try:
            when = datetime.fromtimestamp(self.path.stat().st_mtime, tz=UTC)
        except OSError:
            when = datetime.now(UTC)
        return when.strftime(fmt)

    def document_name(self) -> str:
        """Return ``session-<prefix>`` — the stored document name.

        The SAME stable name the compaction hook files under, so hook and
        backfill write one canonical document per session and ``overwrite=True``
        dedups regardless of which ran first.  A changed transcript overwrites
        that one document rather than accumulating an mtime-suffixed duplicate.
        """
        return f"session-{self.session_prefix}"
