"""Write chunks and their pages in the order that keeps every chunk's page present."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Self, final

from quarry.db.chunk_table import ChunkTable
from quarry.db.page_store import PageStore
from quarry.page_record import PageRecord

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from quarry.db.document_ref import DocumentRef
    from quarry.models import Chunk
    from quarry.types import LanceDB, LanceTable

# Serializes every chunks ``table.add()`` and ``table.delete()`` so the
# progressive sync consumer's flushes never race a producer thread's
# overwrite-delete.  LanceDB's ``add`` is not internally serialized, so the
# single consumer plus this lock are what keep writes conflict-free.
_write_lock = threading.Lock()

# The record key carrying a page's text from the chunker to the pages table;
# it is never a chunks-table column.
PAGE_TEXT_KEY = "page_raw_text"


@final
class ChunkWriter:
    """Add and delete chunk rows together with the pages they belong to.

    A flush puts its pages before its chunks, and a delete removes chunks before
    pages, so a chunk never exists without its page: a crash between the two
    tables leaves only orphan pages, which readers never see and a sweep removes.
    The ordering is safe because one embed job runs at a time
    (``EMBED_CONCURRENCY_CEILING == 1``): no second writer can delete a page
    between its put and its chunk add.
    """

    __slots__ = ("_pages", "_table")

    _pages: PageStore
    _table: ChunkTable

    def __new__(cls, db: LanceDB) -> Self:
        self = super().__new__(cls)
        self._table = ChunkTable(db)
        self._pages = PageStore(db)
        return self

    @staticmethod
    def build_records(
        chunks: list[Chunk], vectors: NDArray[np.float32]
    ) -> list[dict[str, object]]:
        """Build the row dicts of a flush; each carries its page text."""
        return ChunkTable.build_records(chunks, vectors)

    def open_or_none(self) -> LanceTable | None:
        """Return the chunks table, or ``None`` when it does not exist."""
        return self._table.open_or_none()

    def add(self, records: list[dict[str, object]]) -> None:
        """Put the pages of *records*, then append their chunk rows."""
        self._pages.put(self._pages_of(records))
        self._add_chunks(
            [{k: v for k, v in r.items() if k != PAGE_TEXT_KEY} for r in records]
        )

    @property
    def pages(self) -> PageStore:
        """The pages table, for reading a page's text."""
        return self._pages

    def delete_document(self, predicate: str, ref: DocumentRef) -> None:
        """Delete the chunks *predicate* selects, then the pages of *ref*."""
        self._delete_chunks(predicate)
        self._pages.delete(ref)

    def delete_tail(self, predicate: str) -> None:
        """Delete the chunks *predicate* selects, keeping every page."""
        self._delete_chunks(predicate)

    def delete_collection(self, collection: str, clause: str) -> None:
        """Delete the chunks *clause* selects, then the pages of *collection*."""
        self._delete_chunks(clause)
        self._pages.delete_collection(collection)

    @staticmethod
    def _pages_of(records: list[dict[str, object]]) -> list[PageRecord]:
        return [
            PageRecord(
                str(r["collection"]),
                str(r["document_name"]),
                int(str(r["page_number"])),
                str(r[PAGE_TEXT_KEY]),
            )
            for r in records
        ]

    def _add_chunks(self, rows: list[dict[str, object]]) -> None:
        table = self._table.get_or_create(rows)
        if table is not None:
            with _write_lock:
                table.add(rows)

    def _delete_chunks(self, predicate: str) -> None:
        table = self._table.open_or_none()
        if table is not None:
            with _write_lock:
                table.delete(predicate)
