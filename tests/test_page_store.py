"""Tests for PageStore: the pages table holding each page's text once."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pytest

from quarry.db.chunk_store import ChunkStore
from quarry.db.document_ref import CollectionRef, DocumentRef
from quarry.db.orphan_pages import GRACE_PERIOD, OrphanPageSweep
from quarry.db.page_store import PageStore
from quarry.db.row_budget import RowBudgetError
from quarry.models import Chunk
from quarry.page_record import PageRecord

if TYPE_CHECKING:
    from quarry.types import LanceDB


def _page(page: int = 1, text: str = "page text", doc: str = "d.txt") -> PageRecord:
    return PageRecord("col", doc, page, text)


def _chunk(page: int, index: int, doc: str = "d.txt") -> Chunk:
    return Chunk(
        document_name=doc,
        document_path=f"/docs/{doc}",
        collection="col",
        page_number=page,
        total_pages=3,
        chunk_index=index,
        text=f"chunk {index}",
        page_raw_text=f"page {page}",
        page_type="text",
        source_format=".txt",
        ingestion_timestamp=datetime.now(UTC),
    )


class TestPut:
    def test_text_is_retrievable_from_the_pages_table(self, lance_db: LanceDB) -> None:
        store = PageStore(lance_db)
        store.put([_page(1, "alpha"), _page(2, "beta")])
        assert store.text_of("d.txt", 2, collection="col") == "beta"

    def test_a_missing_page_is_none(self, lance_db: LanceDB) -> None:
        store = PageStore(lance_db)
        store.put([_page(1)])
        assert store.text_of("d.txt", 9, collection="col") is None

    def test_no_table_is_none(self, lance_db: LanceDB) -> None:
        assert PageStore(lance_db).text_of("d.txt", 1) is None

    def test_a_page_is_stored_once_however_often_it_is_put(
        self, lance_db: LanceDB
    ) -> None:
        store = PageStore(lance_db)
        store.put([_page(1, "v1")])
        store.put([_page(1, "v2"), _page(1, "v2")])
        assert store.count() == 1
        assert store.text_of("d.txt", 1, collection="col") == "v2"

    def test_the_collection_scopes_a_lookup(self, lance_db: LanceDB) -> None:
        store = PageStore(lance_db)
        store.put(
            [PageRecord("a", "d.txt", 1, "in a"), PageRecord("b", "d.txt", 1, "in b")]
        )
        assert store.text_of("d.txt", 1, collection="b") == "in b"

    def test_an_over_budget_page_is_refused_not_truncated(
        self, lance_db: LanceDB
    ) -> None:
        store = PageStore(lance_db)
        with pytest.raises(RowBudgetError, match="page"):
            store.put([_page(1, "a" * (2**20))])
        assert store.count() == 0


class TestDelete:
    def test_a_document_delete_leaves_other_documents(self, lance_db: LanceDB) -> None:
        store = PageStore(lance_db)
        store.put([_page(1, doc="a.txt"), _page(1, doc="b.txt")])
        store.delete(DocumentRef("a.txt", CollectionRef("col")))
        assert store.count() == 1
        assert store.text_of("b.txt", 1) == "page text"

    def test_a_collection_delete_removes_its_pages(self, lance_db: LanceDB) -> None:
        store = PageStore(lance_db)
        store.put([PageRecord("a", "d.txt", 1, "x"), PageRecord("b", "d.txt", 1, "y")])
        store.delete_collection("a")
        assert store.count() == 1

    def test_deleting_with_no_table_is_a_noop(self, lance_db: LanceDB) -> None:
        PageStore(lance_db).delete(DocumentRef("a.txt"))


class TestOrphanSweep:
    def _age(self, lance_db: LanceDB) -> datetime:
        return datetime.now(UTC) + GRACE_PERIOD + timedelta(minutes=1)

    def test_an_orphan_inside_the_grace_period_is_kept(self, lance_db: LanceDB) -> None:
        PageStore(lance_db).put([_page(1)])
        later = datetime.now(UTC) + GRACE_PERIOD - timedelta(minutes=1)
        assert OrphanPageSweep(lance_db).run(now=later) == 0
        assert PageStore(lance_db).count() == 1

    def test_an_orphan_past_the_grace_period_is_swept(self, lance_db: LanceDB) -> None:
        PageStore(lance_db).put([_page(1)])
        assert OrphanPageSweep(lance_db).run(now=self._age(lance_db)) == 1
        assert PageStore(lance_db).count() == 0

    def test_a_page_with_a_chunk_is_never_swept(self, lance_db: LanceDB) -> None:
        PageStore(lance_db).put([_page(1, "page 1")])
        ChunkStore(lance_db).insert(
            [_chunk(1, 0), _chunk(1, 1)], np.zeros((2, 768), dtype=np.float32)
        )
        assert OrphanPageSweep(lance_db).run(now=self._age(lance_db)) == 0
        assert PageStore(lance_db).count() == 1

    def test_only_the_page_without_chunks_is_swept(self, lance_db: LanceDB) -> None:
        PageStore(lance_db).put([_page(1, "page 1")])
        ChunkStore(lance_db).insert(
            [_chunk(1, 0)], np.zeros((1, 768), dtype=np.float32)
        )
        PageStore(lance_db).put([_page(2, "orphan")])
        assert OrphanPageSweep(lance_db).run(now=self._age(lance_db)) == 1
        assert PageStore(lance_db).text_of("d.txt", 1, collection="col") == "page 1"
        assert PageStore(lance_db).text_of("d.txt", 2, collection="col") is None

    def test_no_pages_table_sweeps_nothing(self, lance_db: LanceDB) -> None:
        assert OrphanPageSweep(lance_db).run() == 0
