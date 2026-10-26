"""Insert a page's chunks; a failed insert of a fetched page leaves no partial copy."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.display_name import DisplayName
from quarry.ingestion.streaming import DocumentStreamer
from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from quarry.ingestion.ingest_context import IngestContext
    from quarry.models import Chunk

logger = logging.getLogger(__name__)


@final
class OwnedInsert:
    """The write of one document's chunks, which cleans up after itself on failure.

    A fetched page always owns its name when the funnel reaches the insert: it was
    free, or an overwrite deleted the prior copy on purpose (a non-overwriting
    ingest of a held page is skipped before this point).  So a failure may delete
    what the insert wrote under the name, and nothing else.  A label keeps whatever
    landed, as before.  The cleanup never replaces the insert's own error.
    """

    __slots__ = ("_context", "_name")

    _name: str
    _context: IngestContext

    def __new__(cls, document_name: str, context: IngestContext) -> Self:
        self = super().__new__(cls)
        self._name = document_name
        self._context = context
        return self

    def run(self, chunks: list[Chunk]) -> int:
        """Embed and insert *chunks*, returning how many were stored."""
        try:
            return DocumentStreamer(self._context.settings).insert(
                chunks, self._context.database.store, self._name
            )
        except Exception:
            if self._context.url_sourced:
                self._discard_partial()
            raise

    def _discard_partial(self) -> None:
        """Delete what a failed insert wrote under the name; log a failure."""
        try:
            self._context.database.store.delete_document(
                self._name, collection=self._context.collection, count=False
            )
        except Exception as exc:
            # A cleanup boundary (PY-EH-6): the caller re-raises the insert's error
            # unconditionally, so whatever the store raises here is only logged,
            # never allowed to replace it.  The log names the class and the frames,
            # not the message: a store's message quotes the delete predicate.
            logger.warning(
                "insert of %s failed and its partial copy could not be removed",
                DisplayName(self._name).redacted(),
                exc_info=RedactedFailure.of(exc),
            )
