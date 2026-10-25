"""The transcripts backfill has found to hold nothing to index, kept across runs."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Self, cast, final

from quarry.atomic_file import AtomicFile

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@final
class UnindexableTranscripts:
    """Remember a transcript that chunked to nothing, as of the mtime it had then.

    A conversation of tool calls only has no text to store, so it is never
    stored and never "current": every run would read it again and find the same
    nothing.  This keeps its mtime in a small JSON file beside the registry, the
    other state backfill reads, so a later run skips it until the transcript
    changes.  The file is rewritten atomically; one that is missing or unreadable
    starts empty, so the cost of losing it is a re-read, never a wrong skip.
    """

    __slots__ = ("_dirty", "_known", "_store")

    _store: Path
    _known: dict[str, int]
    _dirty: bool

    def __new__(cls, store: Path) -> Self:
        self = super().__new__(cls)
        self._store = store
        self._known = cls._load(store)
        self._dirty = False
        return self

    def knows(self, transcript: Path) -> bool:
        """Return whether *transcript* is known to be unindexable and is unchanged."""
        try:
            mtime = transcript.stat().st_mtime_ns
        except OSError:
            return False
        return self._known.get(str(transcript)) == mtime

    def record(self, transcript: Path) -> None:
        """Note that *transcript*, as it is now, holds nothing to index."""
        try:
            self._known[str(transcript)] = transcript.stat().st_mtime_ns
        except OSError:
            return
        self._dirty = True

    def save(self) -> None:
        """Write what was learned this run, if anything was."""
        if not self._dirty:
            return
        AtomicFile(self._store).replace(json.dumps(self._known, sort_keys=True))
        self._dirty = False

    @staticmethod
    def _load(store: Path) -> dict[str, int]:
        """Return the saved mtimes, or an empty map if there is no usable file."""
        try:
            saved_text = AtomicFile(store).read()  # "" when there is no file yet
            raw = json.loads(saved_text) if saved_text else {}
        except (OSError, ValueError):
            logger.warning("backfill: unindexable-transcript file unreadable; ignoring")
            return {}
        if not isinstance(raw, dict):
            return {}
        saved = cast("dict[str, object]", raw)
        return {path: mtime for path, mtime in saved.items() if isinstance(mtime, int)}
