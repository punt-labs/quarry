"""The file the unindexable-transcript cache lives in."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Self, cast, final

from quarry.atomic_file import AtomicFile
from quarry.os_fault import OsFault

logger = logging.getLogger(__name__)

# A file this size holds hundreds of thousands of transcripts; anything larger is
# not ours, and parsing it would cost more than re-reading the transcripts.
_MAX_BYTES = 4 * 1024 * 1024


@final
class UnindexableFile:
    """Load and save the cache's JSON map of transcript path to mtime.

    A file that is missing, oversized, unreadable or of the wrong shape loads as
    empty (with one warning naming the path and why), so the cost of losing it is
    a re-read, never a wrong skip.  Saving is atomic, mode 0600, and reports a
    fault by name instead of raising: the cache is an optimization, so a run is
    never decided by whether it could be saved.
    """

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    def load(self) -> dict[str, int]:
        """Return the saved mtimes, or an empty map if there is no usable file."""
        try:
            if not self._path.is_file():
                return {}
            if self._path.stat().st_size > _MAX_BYTES:
                logger.warning(
                    "backfill: %s is over %d bytes; ignoring", self._path, _MAX_BYTES
                )
                return {}
            raw = json.loads(AtomicFile(self._path).read())
        except (OSError, ValueError, RecursionError) as exc:
            logger.warning(
                "backfill: %s unreadable (%s); ignoring",
                self._path,
                type(exc).__name__,
            )
            return {}
        return self._valid(raw)

    def write(self, known: dict[str, int]) -> str:
        """Save *known*; return the fault's name if that failed, else ``""``."""
        try:
            text = json.dumps(known, sort_keys=True)
            AtomicFile(self._path).replace(text, mode=0o600)
        except OSError as exc:
            fault = OsFault.named(exc)
            logger.warning("backfill: could not save %s: %s", self._path, fault)
            return fault
        return ""

    @staticmethod
    def prune(known: dict[str, int]) -> dict[str, int]:
        """Return *known* without the transcripts that can no longer be examined."""
        live: dict[str, int] = {}
        for path, mtime in known.items():
            try:
                Path(path).stat()
            except OSError:
                continue
            live[path] = mtime
        return live

    def _valid(self, raw: object) -> dict[str, int]:
        """Return the entries of *raw* that are mtimes, saying what was dropped."""
        if not isinstance(raw, dict):
            logger.warning(
                "backfill: %s is not an object (%s); ignoring",
                self._path,
                type(raw).__name__,
            )
            return {}
        saved = cast("dict[str, object]", raw)
        kept = {
            path: mtime
            for path, mtime in saved.items()
            if isinstance(mtime, int) and not isinstance(mtime, bool)
        }
        if len(kept) != len(saved):
            logger.warning(
                "backfill: %s: dropped %d entries that are not mtimes",
                self._path,
                len(saved) - len(kept),
            )
        return kept
