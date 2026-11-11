"""Open a transcript the one safe way: snapshot its version before reading it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.transcript_reader import TranscriptReader
from quarry.transcript_snapshot import TranscriptSnapshot

if TYPE_CHECKING:
    from pathlib import Path


@final
@dataclass(frozen=True, slots=True)
class TranscriptIntake:
    """A transcript's version and its reader, snapshotted before any read.

    ``TranscriptReader.__new__`` reads the file synchronously at construction,
    so stamping a capture's version after building the reader lets a writer
    grow the file in between: the capture then ships stale content tagged with
    a version a later, now-quiescent pass will already match, and that growth
    is silently never ingested (quarry-uq95). Pairing snapshot and reader in
    one factory makes the correct ordering the only ordering a caller can get.
    """

    snapshot: TranscriptSnapshot
    reader: TranscriptReader

    @classmethod
    def taken(cls, path: Path) -> Self:
        """Snapshot *path*'s version, then build its reader."""
        return cls(TranscriptSnapshot.taken(path), TranscriptReader(path))
