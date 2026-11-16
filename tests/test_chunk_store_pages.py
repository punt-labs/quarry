"""ChunkStore writes pages before chunks and deletes them after."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import numpy as np
import pytest

from quarry.db.chunk_store import ChunkStore
from quarry.db.chunk_writer import ChunkWriter
from quarry.db.page_store import PageStore
from quarry.db.schema import TABLE_NAME
from quarry.models import Chunk

if TYPE_CHECKING:
    from collections.abc import Sequence

    from quarry.db.document_ref import DocumentRef
    from quarry.page_record import PageRecord
    from quarry.types import LanceDB

_DIM = 768


def _chunk(page: int, index: int, doc: str = "d.txt") -> Chunk:
    return Chunk(
        document_name=doc,
        document_path=f"/docs/{doc}",
        collection="col",
        page_number=page,
        total_pages=2,
        chunk_index=index,
        text=f"chunk {index}",
        page_raw_text=f"full text of page {page}",
        page_type="text",
        source_format=".txt",
        ingestion_timestamp=datetime.now(UTC),
    )


def _insert(store: ChunkStore, chunks: list[Chunk]) -> None:
    store.insert(chunks, np.zeros((len(chunks), _DIM), dtype=np.float32))


def _chunk_pages(db: LanceDB) -> set[int]:
    rows = (
        db.open_table(TABLE_NAME)
        .search()
        .select(["page_number"])
        .limit(10_000)
        .to_list()
    )
    return {int(cast("int", r["page_number"])) for r in rows}


class TestPagesLiveInTheirOwnTable:
    def test_the_chunks_table_has_no_page_text_column(self, lance_db: LanceDB) -> None:
        _insert(ChunkStore(lance_db), [_chunk(1, 0)])
        names = {f.name for f in lance_db.open_table(TABLE_NAME).schema}
        assert "page_raw_text" not in names

    def test_page_text_is_read_from_the_pages_table(self, lance_db: LanceDB) -> None:
        _insert(ChunkStore(lance_db), [_chunk(2, 0)])
        assert PageStore(lance_db).get_page_text("d.txt", 2) == "full text of page 2"

    def test_a_page_is_stored_once_across_its_chunks(self, lance_db: LanceDB) -> None:
        _insert(ChunkStore(lance_db), [_chunk(1, i) for i in range(5)])
        assert PageStore(lance_db).count() == 1

    def test_pages_arriving_in_later_flushes_do_not_duplicate(
        self, lance_db: LanceDB
    ) -> None:
        store = ChunkStore(lance_db)
        _insert(store, [_chunk(1, 0), _chunk(1, 1)])
        _insert(store, [_chunk(1, 2), _chunk(2, 3)])
        assert PageStore(lance_db).count() == 2


class TestCrashSafety:
    def test_a_crash_between_the_tables_leaves_only_orphan_pages(
        self, lance_db: LanceDB
    ) -> None:
        store = ChunkStore(lance_db)
        with (
            patch.object(ChunkWriter, "_add_chunks", side_effect=OSError("crash")),
            pytest.raises(OSError, match="crash"),
        ):
            _insert(store, [_chunk(1, 0), _chunk(2, 1)])
        assert PageStore(lance_db).count() == 2
        assert store.count() == 0

    def test_every_chunk_has_its_page_after_a_failed_second_flush(
        self, lance_db: LanceDB
    ) -> None:
        store = ChunkStore(lance_db)
        _insert(store, [_chunk(1, 0)])
        with (
            patch.object(ChunkWriter, "_add_chunks", side_effect=OSError("crash")),
            pytest.raises(OSError),
        ):
            _insert(store, [_chunk(2, 1)])
        pages = PageStore(lance_db)
        assert all(pages.get_page_text("d.txt", p) for p in _chunk_pages(lance_db))

    def test_a_crash_deleting_pages_leaves_orphans_not_headless_chunks(
        self, lance_db: LanceDB
    ) -> None:
        store = ChunkStore(lance_db)
        _insert(store, [_chunk(1, 0), _chunk(2, 1)])
        with (
            patch.object(PageStore, "delete", side_effect=OSError("crash")),
            pytest.raises(OSError),
        ):
            store.delete_document("d.txt", "col")
        assert store.count() == 0
        assert PageStore(lance_db).count() == 2

    def test_pages_are_put_before_chunks_and_deleted_after(
        self, lance_db: LanceDB
    ) -> None:
        events: list[str] = []
        real_put, real_add = PageStore.put, ChunkWriter._add_chunks
        real_del_pages, real_del = PageStore.delete, ChunkWriter._delete_chunks

        def put(self: PageStore, pages: Sequence[PageRecord]) -> int:
            events.append("put pages")
            return real_put(self, pages)

        def add(self: ChunkWriter, records: list[dict[str, object]]) -> None:
            events.append("add chunks")
            real_add(self, records)

        def del_pages(self: PageStore, ref: DocumentRef) -> None:
            events.append("delete pages")
            real_del_pages(self, ref)

        def del_chunks(self: ChunkWriter, predicate: str) -> None:
            events.append("delete chunks")
            real_del(self, predicate)

        store = ChunkStore(lance_db)
        with (
            patch.object(PageStore, "put", put),
            patch.object(ChunkWriter, "_add_chunks", add),
            patch.object(PageStore, "delete", del_pages),
            patch.object(ChunkWriter, "_delete_chunks", del_chunks),
        ):
            _insert(store, [_chunk(1, 0)])
            store.delete_document("d.txt", "col")
        assert events == ["put pages", "add chunks", "delete chunks", "delete pages"]


class TestDeletes:
    def test_deleting_a_document_removes_its_pages(self, lance_db: LanceDB) -> None:
        store = ChunkStore(lance_db)
        _insert(store, [_chunk(1, 0), _chunk(1, 0, doc="keep.txt")])
        store.delete_document("d.txt", "col")
        assert PageStore(lance_db).get_page_text("d.txt", 1) is None
        assert PageStore(lance_db).get_page_text("keep.txt", 1) is not None

    def test_deleting_a_collection_removes_its_pages(self, lance_db: LanceDB) -> None:
        store = ChunkStore(lance_db)
        _insert(store, [_chunk(1, 0)])
        store.delete_collection("col")
        assert PageStore(lance_db).count() == 0

    def test_a_tail_delete_keeps_the_pages_the_prefix_needs(
        self, lance_db: LanceDB
    ) -> None:
        from quarry.db.document_ref import DocumentRef

        store = ChunkStore(lance_db)
        _insert(store, [_chunk(1, 0), _chunk(1, 1)])
        store.delete_document_tail(DocumentRef.tail("d.txt", "col", 1))
        assert PageStore(lance_db).get_page_text("d.txt", 1) is not None
