"""The session documents a collection already holds, and whether each is current."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, final

from quarry.transcript import Transcript

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)


@final
class StoredSessions:
    """Tell a session that is stored in full from one that has since grown.

    The compaction hook and backfill file a session under the same
    ``session-<id[:8]>`` name, so one document stands for it whoever wrote it.
    The hook writes mid-session, so that document can stop at the last
    compaction: a transcript modified after its document was stored holds
    conversation the document lacks.  Existing under the name is therefore not
    enough to be skipped; the document must also be at least as new as the
    transcript.
    """

    __slots__ = ("_stored",)

    _stored: dict[str, datetime | None]

    def __new__(cls, documents: Iterable[Mapping[str, object]]) -> Self:
        self = super().__new__(cls)
        self._stored = {
            str(doc["document_name"]): cls._parsed(doc.get("ingestion_timestamp"))
            for doc in documents
        }
        return self

    def is_current(self, transcript: Path) -> bool:
        """Return whether *transcript* is stored and unchanged since it was.

        A stored timestamp that cannot be read, or a transcript that cannot be
        examined, is not current: the session is ingested again rather than
        silently skipped.
        """
        name = Transcript(transcript).document_name()
        if name not in self._stored:
            return False
        stored = self._stored[name]
        if stored is None:
            return False
        try:
            modified = datetime.fromtimestamp(transcript.stat().st_mtime, tz=UTC)
        except OSError:
            return False
        return modified <= stored

    def record(self, transcript: Path) -> None:
        """Note that *transcript* has just been stored, in full."""
        self._stored[Transcript(transcript).document_name()] = datetime.now(tz=UTC)

    @staticmethod
    def _parsed(raw: object) -> datetime | None:
        """Return the stored timestamp as an aware datetime, or ``None``."""
        try:
            when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            logger.warning("unparseable stored ingestion_timestamp; re-ingesting")
            return None
        return when if when.tzinfo else when.replace(tzinfo=UTC)
