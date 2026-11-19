"""Chunk storage: insert, progressive-flush, delete, and count operations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

from quarry.db.chunk_writer import ChunkWriter
from quarry.db.document_ref import CollectionRef, DocumentRef
from quarry.db.schema import TABLE_NAME
from quarry.display_name import DisplayName

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from quarry.db.page_store import PageStore
    from quarry.db.readable_page import ReadablePage
    from quarry.models import Chunk
    from quarry.types import LanceDB, LanceTable

logger = logging.getLogger(__name__)


class ChunkStore:
    """Insert, delete, and count chunks in the LanceDB chunks table."""

    __slots__ = ("_db", "_writer")

    _db: LanceDB
    _writer: ChunkWriter

    def __new__(cls, db: LanceDB) -> Self:
        """Share a database connection with the chunk-and-page writer."""
        self = super().__new__(cls)
        self._db = db
        self._writer = ChunkWriter(db)
        return self

    def _open(self) -> LanceTable | None:
        """Return the chunks table for reads/deletes, or ``None`` if absent."""
        return self._writer.open_or_none()

    def insert(
        self,
        chunks: list[Chunk],
        vectors: NDArray[np.float32],
    ) -> int:
        """Insert chunks with their embedding vectors into LanceDB."""
        return self.insert_records(ChunkWriter.build_records(chunks, vectors))

    def insert_records(self, records: list[dict[str, object]]) -> int:
        """Append pre-built row dicts to LanceDB — the per-flush write primitive.

        The pages the rows belong to are written first, so a chunk never exists
        without its page: a crash between the two writes leaves only an orphan
        page.  Serialized via ``_write_lock`` so a progressive flush never races
        a producer thread's overwrite-delete.
        """
        if not records:
            return 0
        self._writer.add(records)
        # DEBUG per the level policy for operational diagnostics: this fires once
        # per FLUSH WINDOW (progressive commit), so one large document emits it
        # many times.  The per-document totals are logged by the pipeline, which
        # is the user-visible operation.
        logger.debug("Inserted %d chunks into %s", len(records), TABLE_NAME)
        return len(records)

    @property
    def pages(self) -> PageStore:
        """Page text: one row per page, read by document and page number."""
        return self._writer.pages

    @property
    def readable(self) -> ReadablePage:
        """The reader for a page's text: only pages a live chunk still holds."""
        return self._writer.readable

    def delete_document(
        self,
        document_name: str,
        collection: str | None = None,
        *,
        count: bool = True,
    ) -> int:
        """Delete all chunks for a document, optionally scoped to a collection."""
        table = self._open()
        if table is None:
            return 0
        ref = DocumentRef(document_name, CollectionRef.optional(collection))
        predicate = ref.predicate()
        deleted = table.count_rows(predicate) if count else 0
        self._writer.delete_document(predicate, ref)
        shown = DisplayName(document_name).redacted()
        if count:
            logger.info("Deleted %d chunks for %s", deleted, shown)
        else:
            logger.info("Issued chunk delete for %s (counting disabled)", shown)
        return deleted

    def delete_document_tail(self, ref: DocumentRef) -> int:
        """Delete *ref*'s chunks with ``chunk_index >= ref.min_chunk_index``.

        The within-file resume reconciliation primitive (DES-034 §5.3): before
        re-embedding from a watermark ``w``, this clears any possibly-partial
        post-watermark write ``[w, K)`` while preserving the confirmed prefix
        ``[0, w)``, making resume idempotent under repeated crashes. Returns the
        number of rows removed.

        Raises ``ValueError`` if *ref* has no positive watermark (see
        :meth:`DocumentRef.require_tail`); use ``delete_document`` to drop a whole
        document.
        """
        ref.require_tail()
        table = self._open()
        if table is None:
            return 0
        predicate = ref.predicate()
        deleted = table.count_rows(predicate)
        self._writer.delete_tail(predicate)
        logger.info(
            "Delete-tail removed %d chunks for %s (chunk_index >= %d)",
            deleted,
            DisplayName(ref.document_name).redacted(),
            ref.min_chunk_index,
        )
        return deleted

    def delete_collection(self, collection: str) -> int:
        """Delete all chunks in a collection."""
        table = self._open()
        if table is None:
            return 0
        before = table.count_rows()
        self._writer.delete_collection(collection, CollectionRef(collection).clause())
        deleted = before - self.count()
        logger.info("Deleted %d chunks for collection %s", deleted, collection)
        return deleted

    def count(self, collection_filter: str | None = None) -> int:
        """Return the total number of chunks, optionally filtered by collection."""
        table = self._open()
        if table is None:
            return 0
        if collection_filter:
            return table.count_rows(CollectionRef(collection_filter).clause())
        return table.count_rows()
