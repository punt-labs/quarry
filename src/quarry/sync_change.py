"""The one rule for deciding whether a file on disk needs re-embedding.

Two paths reach this question and they must answer it identically: the bulk
:class:`~quarry.sync_planner.SyncPlanner`, which buckets a whole collection
against the registry, and the watch loop's per-file
:class:`~quarry.ingestion.file_indexer.SingleFileIndexer`, which sees one
changed path at a time.  When only the bulk path knew the rule, an fs event on
byte-identical content — an editor's save-in-place, a branch switch restoring
the same bytes, a plain ``touch`` — re-embedded the whole document.

The decision carries the content hash it computed, so a caller that needs to
refresh the registry row does not hash the file a second time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Self, final

from quarry.sync_discovery import FileDiscovery

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.sync_file_store import FileRecord


class FileChange(Enum):
    """What a file on disk needs, relative to its registry row."""

    UNCHANGED = auto()
    """Metadata and content both match — nothing to do at all."""

    REFRESH = auto()
    """``(mtime, size)`` moved but the content hash still matches: row only."""

    REINDEX = auto()
    """New, partial, or genuinely different content — extract and embed."""


@final
@dataclass(frozen=True, slots=True)
class FileChangeDecision:
    """A :class:`FileChange` plus the disk hash that produced it.

    ``content_hash`` is the freshly-read hash on a ``REFRESH`` (the caller
    writes it into the registry row) and ``None`` otherwise — either the hash
    was never needed or it could not be read, and an unreadable hash always
    decides ``REINDEX`` rather than claiming the file is current.
    """

    change: FileChange
    content_hash: str | None = None

    @property
    def needs_embedding(self) -> bool:
        """Return whether this decision requires extracting and embedding."""
        return self.change is FileChange.REINDEX


@final
class FileChangeDetector:
    """Decide what one file needs, given its registry row and its ``stat``.

    Stateless: the rule is a pure function of the row and the file, so one
    detector serves a whole scan.
    """

    __slots__ = ()

    def __new__(cls) -> Self:
        """Create a stateless registry-versus-filesystem change classifier."""
        return super().__new__(cls)

    def classify(
        self, record: FileRecord | None, path: Path, stat: os.stat_result
    ) -> FileChangeDecision:
        """Return what *path* needs relative to *record*.

        A missing or partial row always re-indexes: there is no trustworthy
        prior state to compare against.  Identical ``(mtime, size)`` is taken as
        unchanged without hashing — the cheap check that makes a full scan
        affordable.  Otherwise the content hash decides, and any doubt (no
        stored hash, a size mismatch, an unreadable file) resolves toward
        re-indexing, never toward wrongly declaring the file current.
        """
        if record is None or record.is_partial:
            return FileChangeDecision(FileChange.REINDEX)
        if record.mtime == stat.st_mtime and record.size == stat.st_size:
            return FileChangeDecision(FileChange.UNCHANGED)
        disk_hash = self._matching_hash(record, path, stat)
        if disk_hash is None:
            return FileChangeDecision(FileChange.REINDEX)
        return FileChangeDecision(FileChange.REFRESH, disk_hash)

    @staticmethod
    def _matching_hash(
        record: FileRecord, path: Path, stat: os.stat_result
    ) -> str | None:
        """Return the disk hash when it still matches *record*, else ``None``."""
        if record.content_hash is None or record.size != stat.st_size:
            return None
        try:
            disk_hash = FileDiscovery.content_hash(path)
        except OSError:
            return None
        return disk_hash if disk_hash == record.content_hash else None
