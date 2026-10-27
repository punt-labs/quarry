"""The session documents a collection already holds, and whether each is current."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.transcript_snapshot import TranscriptSnapshot

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)


@final
class StoredSessions:
    """Tell a session that is stored in full from one whose transcript has changed.

    The compaction hook and backfill file a session under the same
    ``session-<id[:8]>`` name, so one document stands for it whoever wrote it.
    The hook writes mid-session, so that document can stop at the last
    compaction.  Backfill stamps each row with the version of a
    :class:`~quarry.transcript_snapshot.TranscriptSnapshot` taken *before* the
    read: a stored version equal to the transcript's current one is current,
    and a transcript that grew while it was read or stored no longer matches.
    A row with no version (a hook capture, or one stored before the column
    existed) cannot vouch for being complete, so its session is ingested again
    rather than skipped.
    """

    __slots__ = ("_stored",)

    _stored: dict[str, str]

    def __new__(cls, documents: Iterable[Mapping[str, object]]) -> Self:
        self = super().__new__(cls)
        self._stored = {
            name: str(doc.get("source_version") or "")
            for doc in documents
            if (name := str(doc["document_name"])).startswith("session-")
        }
        return self

    @staticmethod
    def snapshot(transcript: Path) -> TranscriptSnapshot:
        """Snapshot *transcript* now, before it is read; see :meth:`is_current`."""
        return TranscriptSnapshot.taken(transcript)

    def is_current(self, snapshot: TranscriptSnapshot) -> bool:
        """Return whether *snapshot*'s session is stored at *snapshot*'s version.

        A transcript that could not be examined, or an unversioned row, is
        never current.
        """
        stored = self._stored.get(snapshot.document_name(), "")
        return bool(snapshot.version) and stored == snapshot.version

    def record(self, snapshot: TranscriptSnapshot) -> None:
        """Note that *snapshot*'s session has just been stored, in full, at it."""
        self._stored[snapshot.document_name()] = snapshot.version
