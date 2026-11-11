"""A transcript capture already held at its sender's version, and its skip."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.stored_sessions import StoredSessions

if TYPE_CHECKING:
    from quarry.db import Database
    from quarry.results import IngestResult

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class UnchangedTranscript:
    """A transcript the store already holds at the version its sender just read.

    Daemon-side on purpose (consults the collection's own catalog, not a
    client-side cache of what was last sent): two concurrent captures of the
    same session cannot both read a stale "already sent" answer this way,
    since the comparison reads the catalog fresh on the collection's single
    FIFO writer.  Mirrors :class:`~quarry.ingestion.existing_page.ExistingPage`
    — a held document and the skip a caller reports for it — but answers a
    different question: "is this exact version already stored?", not "is ANY
    copy already stored?".
    """

    name: str
    collection: str

    @classmethod
    def look_for(
        cls, name: str, version: str, collection: str, database: Database
    ) -> Self | None:
        """Return the held transcript that makes a capture of *version* a skip.

        ``""`` means the sender took no snapshot (an older client, or a
        non-transcript capture), so the caller must always ingest; ``None`` is
        this lookup's own contract for every other case nothing currently
        vouches for (PY-EH-8): the caller writes.
        """
        if not version:
            return None
        docs = database.catalog.list_documents(collection_filter=collection)
        if not StoredSessions(docs).is_current_version(name, version):
            return None
        return cls(name, collection)

    def skip(self) -> IngestResult:
        """Log the skip and return the result an unchanged capture reports."""
        logger.info(
            "%s: transcript unchanged since the last capture; skipping re-ingest",
            self.name,
        )
        return {
            "document_name": self.name,
            "collection": self.collection,
            "chunks": 0,
            "skipped": "unchanged",
        }
