"""Tests for ChunkStore: insert_records, build_records, delete_document_tail."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import numpy as np
import pytest

from quarry.db.chunk_store import ChunkStore
from quarry.db.chunk_table import ChunkTable, DocumentRef
from quarry.db.schema import TABLE_NAME
from quarry.models import Chunk

if TYPE_CHECKING:
    from quarry.types import LanceDB

_DIM = 768


def _chunk(
    index: int, *, document_name: str = "doc.txt", collection: str = "col"
) -> Chunk:
    return Chunk(
        document_name=document_name,
        document_path=f"/docs/{document_name}",
        collection=collection,
        page_number=1,
        total_pages=1,
        chunk_index=index,
        text=f"chunk {index}",
        page_raw_text="raw",
        page_type="text",
        source_format=".txt",
        ingestion_timestamp=datetime.now(UTC),
    )


def _seed(store: ChunkStore, total: int, **kw: str) -> None:
    chunks = [_chunk(i, **kw) for i in range(total)]
    vectors = np.zeros((total, _DIM), dtype=np.float32)
    store.insert(chunks, vectors)


def _chunk_indexes(db: LanceDB, document_name: str) -> list[int]:
    table = db.open_table(TABLE_NAME)
    rows = (
        table.search()
        .where(f"document_name = '{document_name}'")
        .select(["chunk_index"])
        .limit(10_000)
        .to_list()
    )
    return sorted(cast("int", r["chunk_index"]) for r in rows)


class TestInsertRecords:
    def test_empty_records_returns_zero_no_table(self, lance_db: LanceDB) -> None:
        assert ChunkStore(lance_db).insert_records([]) == 0

    def test_build_records_carries_vector_and_fields(self, lance_db: LanceDB) -> None:
        records = ChunkTable.build_records(
            [_chunk(0)], np.ones((1, _DIM), dtype=np.float32)
        )
        assert len(records) == 1
        assert records[0]["chunk_index"] == 0
        assert len(records[0]["vector"]) == _DIM  # type: ignore[arg-type]

    def test_insert_records_persists(self, lance_db: LanceDB) -> None:
        store = ChunkStore(lance_db)
        records = ChunkTable.build_records(
            [_chunk(0), _chunk(1)], np.zeros((2, _DIM), dtype=np.float32)
        )
        assert store.insert_records(records) == 2
        assert store.count() == 2


class TestDeleteDocumentTail:
    def test_removes_tail_preserves_prefix(self, lance_db: LanceDB) -> None:
        store = ChunkStore(lance_db)
        _seed(store, total=10)
        removed = store.delete_document_tail(DocumentRef("doc.txt", "col", 6))
        assert removed == 4
        assert _chunk_indexes(lance_db, "doc.txt") == [0, 1, 2, 3, 4, 5]

    def test_zero_watermark_rejected_not_whole_document(
        self, lance_db: LanceDB
    ) -> None:
        """A tail delete with min_chunk_index 0 must raise, never wipe the doc."""
        store = ChunkStore(lance_db)
        _seed(store, total=5)
        with pytest.raises(ValueError, match="positive watermark"):
            store.delete_document_tail(DocumentRef("doc.txt", "col", 0))
        assert _chunk_indexes(lance_db, "doc.txt") == [0, 1, 2, 3, 4]  # untouched

    def test_watermark_at_total_removes_nothing(self, lance_db: LanceDB) -> None:
        store = ChunkStore(lance_db)
        _seed(store, total=5)
        removed = store.delete_document_tail(DocumentRef("doc.txt", "col", 5))
        assert removed == 0
        assert _chunk_indexes(lance_db, "doc.txt") == [0, 1, 2, 3, 4]

    def test_scoped_to_document(self, lance_db: LanceDB) -> None:
        store = ChunkStore(lance_db)
        _seed(store, total=4, document_name="a.txt")
        _seed(store, total=4, document_name="b.txt")
        store.delete_document_tail(DocumentRef("a.txt", "col", 2))
        assert _chunk_indexes(lance_db, "a.txt") == [0, 1]
        assert _chunk_indexes(lance_db, "b.txt") == [0, 1, 2, 3]

    def test_no_table_returns_zero(self, lance_db: LanceDB) -> None:
        assert (
            ChunkStore(lance_db).delete_document_tail(DocumentRef("doc.txt", "col", 1))
            == 0
        )


class TestDocumentRefInvariants:
    def test_rejects_empty_document_name(self) -> None:
        with pytest.raises(ValueError, match="document_name must be non-empty"):
            DocumentRef("", "col")

    def test_rejects_negative_watermark(self) -> None:
        with pytest.raises(ValueError, match="min_chunk_index must be >= 0"):
            DocumentRef("doc.txt", "col", -1)

    def test_zero_watermark_is_a_valid_whole_document_ref(self) -> None:
        # min 0 is legal for a whole-document selector (delete_document uses it);
        # only delete_document_tail additionally requires a positive watermark.
        ref = DocumentRef("doc.txt", "col")
        assert ref.min_chunk_index == 0
        assert "chunk_index" not in ref.predicate()
