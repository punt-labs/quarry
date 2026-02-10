from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

from quarry.database import (
    count_chunks,
    create_collection_index,
    delete_collection,
    delete_document,
    get_db,
    get_page_text,
    insert_chunks,
    list_collections,
    list_documents,
    optimize_table,
    search,
)
from quarry.models import Chunk


def _make_chunk(
    page_number: int = 1,
    chunk_index: int = 0,
    text: str = "test chunk text",
    document_name: str = "test.pdf",
    collection: str = "default",
) -> Chunk:
    return Chunk(
        document_name=document_name,
        document_path="/tmp/test.pdf",
        collection=collection,
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

    def test_delete_scoped_to_collection(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, document_name="a.pdf", collection="c1"),
            _make_chunk(chunk_index=0, document_name="a.pdf", collection="c2"),
        ]
        vectors = _random_vectors(2)
        insert_chunks(db, chunks, vectors)

        deleted = delete_document(db, "a.pdf", collection="c1")
        assert deleted == 1
        assert count_chunks(db) == 1


class TestSearchWithCollection:
    def test_collection_filter(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, text="alpha", collection="math"),
            _make_chunk(chunk_index=0, text="beta", collection="science"),
        ]
        vectors = _random_vectors(2)
        insert_chunks(db, chunks, vectors)

        results = search(db, vectors[0], limit=10, collection_filter="math")
        collections = {r["collection"] for r in results}
        assert collections == {"math"}

    def test_collection_and_document_filter(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(
                chunk_index=0,
                document_name="a.pdf",
                text="one",
                collection="math",
            ),
            _make_chunk(
                chunk_index=0,
                document_name="b.pdf",
                text="two",
                collection="math",
            ),
            _make_chunk(
                chunk_index=0,
                document_name="a.pdf",
                text="three",
                collection="science",
            ),
        ]
        vectors = _random_vectors(3)
        insert_chunks(db, chunks, vectors)

        results = search(
            db,
            vectors[0],
            limit=10,
            document_filter="a.pdf",
            collection_filter="math",
        )
        assert len(results) == 1
        assert results[0]["collection"] == "math"
        assert results[0]["document_name"] == "a.pdf"

    def test_no_results_for_unknown_collection(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=0, collection="math")]
        vectors = _random_vectors(1)
        insert_chunks(db, chunks, vectors)

        results = search(db, vectors[0], limit=10, collection_filter="unknown")
        assert results == []


class TestListDocumentsWithCollection:
    def test_filter_by_collection(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(document_name="a.pdf", collection="math"),
            _make_chunk(document_name="b.pdf", collection="science"),
        ]
        vectors = _random_vectors(2)
        insert_chunks(db, chunks, vectors)

        docs = list_documents(db, collection_filter="math")
        assert len(docs) == 1
        assert docs[0]["document_name"] == "a.pdf"
        assert docs[0]["collection"] == "math"

    def test_includes_collection_in_output(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(document_name="a.pdf", collection="ml-101")]
        vectors = _random_vectors(1)
        insert_chunks(db, chunks, vectors)

        docs = list_documents(db)
        assert docs[0]["collection"] == "ml-101"


class TestCountChunksWithCollection:
    def test_count_filtered(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, collection="math"),
            _make_chunk(chunk_index=1, collection="math"),
            _make_chunk(chunk_index=0, collection="science"),
        ]
        vectors = _random_vectors(3)
        insert_chunks(db, chunks, vectors)

        assert count_chunks(db, collection_filter="math") == 2
        assert count_chunks(db, collection_filter="science") == 1
        assert count_chunks(db) == 3


class TestListCollections:
    def test_lists_collections(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, document_name="a.pdf", collection="math"),
            _make_chunk(chunk_index=1, document_name="a.pdf", collection="math"),
            _make_chunk(chunk_index=0, document_name="b.pdf", collection="math"),
            _make_chunk(chunk_index=0, document_name="c.pdf", collection="science"),
        ]
        vectors = _random_vectors(4)
        insert_chunks(db, chunks, vectors)

        cols = list_collections(db)
        assert len(cols) == 2

        math = next(c for c in cols if c["collection"] == "math")
        assert math["document_count"] == 2
        assert math["chunk_count"] == 3

        science = next(c for c in cols if c["collection"] == "science")
        assert science["document_count"] == 1
        assert science["chunk_count"] == 1

    def test_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        assert list_collections(db) == []


class TestDeleteCollection:
    def test_deletes_collection(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, collection="math"),
            _make_chunk(chunk_index=1, collection="math"),
            _make_chunk(chunk_index=0, collection="science"),
        ]
        vectors = _random_vectors(3)
        insert_chunks(db, chunks, vectors)

        deleted = delete_collection(db, "math")
        assert deleted == 2
        assert count_chunks(db) == 1

    def test_delete_nonexistent_collection(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(collection="math")]
        vectors = _random_vectors(1)
        insert_chunks(db, chunks, vectors)

        deleted = delete_collection(db, "unknown")
        assert deleted == 0
        assert count_chunks(db) == 1

    def test_delete_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        deleted = delete_collection(db, "anything")
        assert deleted == 0


class TestCreateCollectionIndex:
    def test_creates_index_on_populated_table(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=i, collection="math") for i in range(3)]
        vectors = _random_vectors(3)
        insert_chunks(db, chunks, vectors)

        # Should not raise
        create_collection_index(db)

        # Search still works after index creation
        results = search(db, vectors[0], limit=5, collection_filter="math")
        assert len(results) == 3

    def test_noop_on_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        create_collection_index(db)  # No table, no error


class TestOptimizeTable:
    def test_optimizes_populated_table(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=i) for i in range(5)]
        vectors = _random_vectors(5)
        insert_chunks(db, chunks, vectors)

        # Should not raise
        optimize_table(db)

        # Data still accessible
        assert count_chunks(db) == 5

    def test_noop_on_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        optimize_table(db)  # No table, no error


class TestConcurrentInsert:
    def test_parallel_insert_on_fresh_db(self, tmp_path: Path):
        """Multiple threads calling insert_chunks on a fresh DB must not race."""
        db = get_db(tmp_path / "db")
        num_workers = 4
        tasks = [
            [_make_chunk(chunk_index=i, document_name=f"doc{w}.pdf")]
            for w in range(num_workers)
            for i in range(2)
        ]
        all_vectors = _random_vectors(len(tasks))
        task_vectors = [all_vectors[i : i + 1] for i in range(len(tasks))]

        errors: list[Exception] = []

        def _insert(idx: int) -> int:
            return insert_chunks(db, tasks[idx], task_vectors[idx])

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {executor.submit(_insert, i): i for i in range(len(tasks))}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        assert errors == [], f"Concurrent insert raised: {errors}"
        assert count_chunks(db) == len(tasks)
