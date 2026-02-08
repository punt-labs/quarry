from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

from quarry.database import (
    count_chunks,
    delete_document,
    get_db,
    get_page_text,
    insert_chunks,
    list_documents,
    search,
)
from quarry.models import Chunk


def _make_chunk(
    page_number: int = 1,
    chunk_index: int = 0,
    text: str = "test chunk text",
    document_name: str = "test.pdf",
) -> Chunk:
    return Chunk(
        document_name=document_name,
        document_path="/tmp/test.pdf",
        page_number=page_number,
        total_pages=5,
        chunk_index=chunk_index,
        text=text,
        page_raw_text=f"raw text page {page_number}",
        ingestion_timestamp=datetime.now(tz=UTC),
    )


def _random_vectors(n: int, dim: int = 768) -> NDArray[np.float32]:
    rng = np.random.default_rng(42)
    vecs = rng.standard_normal((n, dim)).astype(np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / norms  # type: ignore[no-any-return]


class TestInsertAndSearch:
    def test_insert_returns_count(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=i) for i in range(3)]
        vectors = _random_vectors(3)
        count = insert_chunks(db, chunks, vectors)
        assert count == 3

    def test_search_returns_results(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=0, text="financial report 2024")]
        vectors = _random_vectors(1)
        insert_chunks(db, chunks, vectors)

        results = search(db, vectors[0], limit=5)
        assert len(results) >= 1
        assert results[0]["text"] == "financial report 2024"

    def test_search_empty_table(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        query = _random_vectors(1)[0]
        results = search(db, query, limit=5)
        assert results == []

    def test_search_with_document_filter(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, document_name="a.pdf", text="alpha"),
            _make_chunk(chunk_index=0, document_name="b.pdf", text="beta"),
        ]
        vectors = _random_vectors(2)
        insert_chunks(db, chunks, vectors)

        results = search(db, vectors[0], limit=10, document_filter="a.pdf")
        doc_names = {r["document_name"] for r in results}
        assert doc_names == {"a.pdf"}

    def test_search_limit(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=i) for i in range(5)]
        vectors = _random_vectors(5)
        insert_chunks(db, chunks, vectors)

        results = search(db, vectors[0], limit=2)
        assert len(results) == 2


class TestGetPageText:
    def test_returns_page_text(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(page_number=3)]
        vectors = _random_vectors(1)
        insert_chunks(db, chunks, vectors)

        text = get_page_text(db, "test.pdf", 3)
        assert text == "raw text page 3"

    def test_returns_none_for_missing(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(page_number=1)]
        vectors = _random_vectors(1)
        insert_chunks(db, chunks, vectors)

        text = get_page_text(db, "test.pdf", 99)
        assert text is None

    def test_returns_none_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        text = get_page_text(db, "test.pdf", 1)
        assert text is None


class TestListDocuments:
    def test_lists_documents(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(page_number=1, chunk_index=0, document_name="a.pdf"),
            _make_chunk(page_number=1, chunk_index=1, document_name="a.pdf"),
            _make_chunk(page_number=2, chunk_index=0, document_name="a.pdf"),
        ]
        vectors = _random_vectors(3)
        insert_chunks(db, chunks, vectors)

        docs = list_documents(db)
        assert len(docs) == 1
        assert docs[0]["document_name"] == "a.pdf"
        assert docs[0]["chunk_count"] == 3
        assert docs[0]["indexed_pages"] == 2

    def test_multiple_documents(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(document_name="a.pdf"),
            _make_chunk(document_name="b.pdf"),
        ]
        vectors = _random_vectors(2)
        insert_chunks(db, chunks, vectors)

        docs = list_documents(db)
        names = {str(d["document_name"]) for d in docs}
        assert names == {"a.pdf", "b.pdf"}

    def test_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        docs = list_documents(db)
        assert docs == []


class TestCountChunks:
    def test_counts_populated_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=i) for i in range(5)]
        vectors = _random_vectors(5)
        insert_chunks(db, chunks, vectors)
        assert count_chunks(db) == 5

    def test_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        assert count_chunks(db) == 0

    def test_count_after_delete(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, document_name="a.pdf"),
            _make_chunk(chunk_index=1, document_name="a.pdf"),
            _make_chunk(chunk_index=0, document_name="b.pdf"),
        ]
        vectors = _random_vectors(3)
        insert_chunks(db, chunks, vectors)
        delete_document(db, "a.pdf")
        assert count_chunks(db) == 1


class TestDeleteDocument:
    def test_deletes_document(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, document_name="a.pdf"),
            _make_chunk(chunk_index=1, document_name="a.pdf"),
            _make_chunk(chunk_index=0, document_name="b.pdf"),
        ]
        vectors = _random_vectors(3)
        insert_chunks(db, chunks, vectors)

        deleted = delete_document(db, "a.pdf")
        assert deleted == 2

        docs = list_documents(db)
        names = {str(d["document_name"]) for d in docs}
        assert names == {"b.pdf"}

    def test_delete_nonexistent(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(document_name="a.pdf")]
        vectors = _random_vectors(1)
        insert_chunks(db, chunks, vectors)

        deleted = delete_document(db, "nonexistent.pdf")
        assert deleted == 0

    def test_delete_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        deleted = delete_document(db, "anything.pdf")
        assert deleted == 0

    def test_delete_document_with_single_quote(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=0, document_name="O'Reilly.pdf")]
        vectors = _random_vectors(1)
        insert_chunks(db, chunks, vectors)

        deleted = delete_document(db, "O'Reilly.pdf")
        assert deleted == 1
        assert list_documents(db) == []
