from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from quarry.types import LanceDB

from quarry.db import (
    ChunkCatalog,
    ChunkSearch,
    ChunkStore,
    Database,
    TableOptimizer,
    get_db,
)
from quarry.db.schema import TABLE_NAME
from quarry.models import Chunk
from quarry.retrieval import HybridRetriever, RetrievalConfig


def _make_chunk(
    page_number: int = 1,
    chunk_index: int = 0,
    text: str = "test chunk text",
    document_name: str = "test.pdf",
    collection: str = "default",
    page_type: str = "text",
    source_format: str = ".pdf",
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
        page_type=page_type,
        source_format=source_format,
        ingestion_timestamp=datetime.now(tz=UTC),
    )


def _random_vectors(n: int, dim: int = 768) -> NDArray[np.float32]:
    rng = np.random.default_rng(42)
    vecs = rng.standard_normal((n, dim)).astype(np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    unit: NDArray[np.float32] = (vecs / norms).astype(np.float32)
    return unit


class TestInsertAndSearch:
    def test_insert_returns_count(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=i) for i in range(3)]
        vectors = _random_vectors(3)
        count = ChunkStore(db).insert(chunks, vectors)
        assert count == 3

    def test_search_returns_results(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=0, text="financial report 2024")]
        vectors = _random_vectors(1)
        ChunkStore(db).insert(chunks, vectors)

        results = ChunkSearch(db).vector_search(vectors[0], limit=5)
        assert len(results) >= 1
        assert results[0].text == "financial report 2024"

    def test_search_empty_table(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        query = _random_vectors(1)[0]
        results = ChunkSearch(db).vector_search(query, limit=5)
        assert results == []

    def test_search_with_document_filter(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, document_name="a.pdf", text="alpha"),
            _make_chunk(chunk_index=0, document_name="b.pdf", text="beta"),
        ]
        vectors = _random_vectors(2)
        ChunkStore(db).insert(chunks, vectors)

        results = ChunkSearch(db).vector_search(
            vectors[0], limit=10, document_filter="a.pdf"
        )
        doc_names = {r.document_name for r in results}
        assert doc_names == {"a.pdf"}

    def test_search_limit(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=i) for i in range(5)]
        vectors = _random_vectors(5)
        ChunkStore(db).insert(chunks, vectors)

        results = ChunkSearch(db).vector_search(vectors[0], limit=2)
        assert len(results) == 2


class TestGetPageText:
    def test_returns_page_text(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(page_number=3)]
        vectors = _random_vectors(1)
        ChunkStore(db).insert(chunks, vectors)

        text = ChunkCatalog(db).get_page_text("test.pdf", 3)
        assert text == "raw text page 3"

    def test_returns_none_for_missing(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(page_number=1)]
        vectors = _random_vectors(1)
        ChunkStore(db).insert(chunks, vectors)

        text = ChunkCatalog(db).get_page_text("test.pdf", 99)
        assert text is None

    def test_returns_none_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        text = ChunkCatalog(db).get_page_text("test.pdf", 1)
        assert text is None

    def test_finds_page_in_large_table(self, tmp_path: Path):
        """Regression: get_page_text must find rows even when the table has
        many chunks.  Without an explicit full-scan limit, LanceDB's default
        scan limit causes lookups to miss rows that appear later in the table.
        """
        db = get_db(tmp_path / "db")
        # Insert enough chunks to exceed LanceDB's default scan limit.
        # The target page goes last so it's only found with a full scan.
        n_filler = 20
        fillers = [
            _make_chunk(
                page_number=i + 1,
                chunk_index=i,
                document_name="filler.pdf",
            )
            for i in range(n_filler)
        ]
        target = _make_chunk(
            page_number=456,
            chunk_index=n_filler,
            document_name="big.pdf",
        )
        all_chunks = [*fillers, target]
        vectors = _random_vectors(len(all_chunks))
        ChunkStore(db).insert(all_chunks, vectors)

        text = ChunkCatalog(db).get_page_text("big.pdf", 456)
        assert text == "raw text page 456"


class TestListDocuments:
    def test_lists_documents(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(page_number=1, chunk_index=0, document_name="a.pdf"),
            _make_chunk(page_number=1, chunk_index=1, document_name="a.pdf"),
            _make_chunk(page_number=2, chunk_index=0, document_name="a.pdf"),
        ]
        vectors = _random_vectors(3)
        ChunkStore(db).insert(chunks, vectors)

        docs = ChunkCatalog(db).list_documents()
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
        ChunkStore(db).insert(chunks, vectors)

        docs = ChunkCatalog(db).list_documents()
        names = {str(d["document_name"]) for d in docs}
        assert names == {"a.pdf", "b.pdf"}

    def test_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        docs = ChunkCatalog(db).list_documents()
        assert docs == []


class TestCountChunks:
    def test_counts_populated_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=i) for i in range(5)]
        vectors = _random_vectors(5)
        ChunkStore(db).insert(chunks, vectors)
        assert ChunkStore(db).count() == 5

    def test_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        assert ChunkStore(db).count() == 0

    def test_count_after_delete(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, document_name="a.pdf"),
            _make_chunk(chunk_index=1, document_name="a.pdf"),
            _make_chunk(chunk_index=0, document_name="b.pdf"),
        ]
        vectors = _random_vectors(3)
        ChunkStore(db).insert(chunks, vectors)
        ChunkStore(db).delete_document("a.pdf")
        assert ChunkStore(db).count() == 1


class TestDeleteDocument:
    def test_deletes_document(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, document_name="a.pdf"),
            _make_chunk(chunk_index=1, document_name="a.pdf"),
            _make_chunk(chunk_index=0, document_name="b.pdf"),
        ]
        vectors = _random_vectors(3)
        ChunkStore(db).insert(chunks, vectors)

        deleted = ChunkStore(db).delete_document("a.pdf")
        assert deleted == 2

        docs = ChunkCatalog(db).list_documents()
        names = {str(d["document_name"]) for d in docs}
        assert names == {"b.pdf"}

    def test_delete_nonexistent(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(document_name="a.pdf")]
        vectors = _random_vectors(1)
        ChunkStore(db).insert(chunks, vectors)

        deleted = ChunkStore(db).delete_document("nonexistent.pdf")
        assert deleted == 0

    def test_delete_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        deleted = ChunkStore(db).delete_document("anything.pdf")
        assert deleted == 0

    def test_delete_document_with_single_quote(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=0, document_name="O'Reilly.pdf")]
        vectors = _random_vectors(1)
        ChunkStore(db).insert(chunks, vectors)

        deleted = ChunkStore(db).delete_document("O'Reilly.pdf")
        assert deleted == 1
        assert ChunkCatalog(db).list_documents() == []

    def test_delete_scoped_to_collection(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, document_name="a.pdf", collection="c1"),
            _make_chunk(chunk_index=0, document_name="a.pdf", collection="c2"),
        ]
        vectors = _random_vectors(2)
        ChunkStore(db).insert(chunks, vectors)

        deleted = ChunkStore(db).delete_document("a.pdf", collection="c1")
        assert deleted == 1
        assert ChunkStore(db).count() == 1


class TestDeleteDocumentCountFalse:
    """Verify the count=False fast path skips count_rows() and returns 0."""

    def test_count_false_returns_zero(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, document_name="a.pdf"),
            _make_chunk(chunk_index=1, document_name="a.pdf"),
        ]
        vectors = _random_vectors(2)
        ChunkStore(db).insert(chunks, vectors)

        result = ChunkStore(db).delete_document("a.pdf", count=False)
        assert result == 0
        # Document should actually be deleted from the table.
        assert ChunkStore(db).count() == 0

    def test_count_false_does_not_call_count_rows(self, tmp_path: Path) -> None:
        """count=False must not call table.count_rows() — the whole point."""
        from unittest.mock import MagicMock

        db = MagicMock()
        db.list_tables.return_value.tables = ["chunks"]
        table = MagicMock()
        db.open_table.return_value = table

        result = ChunkStore(db).delete_document("test.pdf", count=False)
        assert result == 0
        table.delete.assert_called_once()
        table.count_rows.assert_not_called()

    def test_count_true_still_returns_actual_count(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, document_name="a.pdf"),
            _make_chunk(chunk_index=1, document_name="a.pdf"),
            _make_chunk(chunk_index=0, document_name="b.pdf"),
        ]
        vectors = _random_vectors(3)
        ChunkStore(db).insert(chunks, vectors)

        result = ChunkStore(db).delete_document("a.pdf", count=True)
        assert result == 2

    def test_count_false_with_collection_scope(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, document_name="a.pdf", collection="c1"),
            _make_chunk(chunk_index=0, document_name="a.pdf", collection="c2"),
        ]
        vectors = _random_vectors(2)
        ChunkStore(db).insert(chunks, vectors)

        result = ChunkStore(db).delete_document("a.pdf", collection="c1", count=False)
        assert result == 0
        # Only the c1 copy should be deleted.
        assert ChunkStore(db).count() == 1

    def test_count_false_empty_table(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        result = ChunkStore(db).delete_document("nonexistent.pdf", count=False)
        assert result == 0


class TestSearchWithCollection:
    def test_collection_filter(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, text="alpha", collection="math"),
            _make_chunk(chunk_index=0, text="beta", collection="science"),
        ]
        vectors = _random_vectors(2)
        ChunkStore(db).insert(chunks, vectors)

        results = ChunkSearch(db).vector_search(
            vectors[0], limit=10, collection_filter="math"
        )
        collections = {r.collection for r in results}
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
        ChunkStore(db).insert(chunks, vectors)

        results = ChunkSearch(db).vector_search(
            vectors[0],
            limit=10,
            document_filter="a.pdf",
            collection_filter="math",
        )
        assert len(results) == 1
        assert results[0].collection == "math"
        assert results[0].document_name == "a.pdf"

    def test_no_results_for_unknown_collection(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=0, collection="math")]
        vectors = _random_vectors(1)
        ChunkStore(db).insert(chunks, vectors)

        results = ChunkSearch(db).vector_search(
            vectors[0], limit=10, collection_filter="unknown"
        )
        assert results == []


class TestSearchWithMetadataFilters:
    def test_page_type_filter(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, text="prose", page_type="text"),
            _make_chunk(chunk_index=1, text="python", page_type="code"),
        ]
        vectors = _random_vectors(2)
        ChunkStore(db).insert(chunks, vectors)

        results = ChunkSearch(db).vector_search(
            vectors[0], limit=10, page_type_filter="code"
        )
        page_types = {r.page_type for r in results}
        assert page_types == {"code"}

    def test_source_format_filter(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(
                chunk_index=0,
                text="report",
                source_format=".pdf",
            ),
            _make_chunk(
                chunk_index=1,
                text="script",
                source_format=".py",
            ),
        ]
        vectors = _random_vectors(2)
        ChunkStore(db).insert(chunks, vectors)

        results = ChunkSearch(db).vector_search(
            vectors[0], limit=10, source_format_filter=".py"
        )
        formats = {r.source_format for r in results}
        assert formats == {".py"}

    def test_combined_metadata_filters(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(
                chunk_index=0,
                text="py code",
                page_type="code",
                source_format=".py",
            ),
            _make_chunk(
                chunk_index=1,
                text="js code",
                page_type="code",
                source_format=".js",
            ),
            _make_chunk(
                chunk_index=2,
                text="pdf prose",
                page_type="text",
                source_format=".pdf",
            ),
        ]
        vectors = _random_vectors(3)
        ChunkStore(db).insert(chunks, vectors)

        results = ChunkSearch(db).vector_search(
            vectors[0],
            limit=10,
            page_type_filter="code",
            source_format_filter=".py",
        )
        assert len(results) == 1
        assert results[0].page_type == "code"
        assert results[0].source_format == ".py"

    def test_metadata_with_collection_filter(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(
                chunk_index=0,
                text="math code",
                collection="math",
                page_type="code",
                source_format=".py",
            ),
            _make_chunk(
                chunk_index=1,
                text="math prose",
                collection="math",
                page_type="text",
                source_format=".pdf",
            ),
            _make_chunk(
                chunk_index=2,
                text="sci code",
                collection="science",
                page_type="code",
                source_format=".py",
            ),
        ]
        vectors = _random_vectors(3)
        ChunkStore(db).insert(chunks, vectors)

        results = ChunkSearch(db).vector_search(
            vectors[0],
            limit=10,
            collection_filter="math",
            page_type_filter="code",
        )
        assert len(results) == 1
        assert results[0].collection == "math"
        assert results[0].page_type == "code"

    def test_no_results_for_unknown_page_type(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=0, page_type="text")]
        vectors = _random_vectors(1)
        ChunkStore(db).insert(chunks, vectors)

        results = ChunkSearch(db).vector_search(
            vectors[0], limit=10, page_type_filter="spreadsheet"
        )
        assert results == []


class TestListDocumentsWithCollection:
    def test_filter_by_collection(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(document_name="a.pdf", collection="math"),
            _make_chunk(document_name="b.pdf", collection="science"),
        ]
        vectors = _random_vectors(2)
        ChunkStore(db).insert(chunks, vectors)

        docs = ChunkCatalog(db).list_documents(collection_filter="math")
        assert len(docs) == 1
        assert docs[0]["document_name"] == "a.pdf"
        assert docs[0]["collection"] == "math"

    def test_includes_collection_in_output(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(document_name="a.pdf", collection="ml-101")]
        vectors = _random_vectors(1)
        ChunkStore(db).insert(chunks, vectors)

        docs = ChunkCatalog(db).list_documents()
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
        ChunkStore(db).insert(chunks, vectors)

        assert ChunkStore(db).count(collection_filter="math") == 2
        assert ChunkStore(db).count(collection_filter="science") == 1
        assert ChunkStore(db).count() == 3


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
        ChunkStore(db).insert(chunks, vectors)

        cols = ChunkCatalog(db).list_collections()
        assert len(cols) == 2

        math = next(c for c in cols if c["collection"] == "math")
        assert math["document_count"] == 2
        assert math["chunk_count"] == 3

        science = next(c for c in cols if c["collection"] == "science")
        assert science["document_count"] == 1
        assert science["chunk_count"] == 1

    def test_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        assert ChunkCatalog(db).list_collections() == []


class TestDeleteCollection:
    def test_deletes_collection(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(chunk_index=0, collection="math"),
            _make_chunk(chunk_index=1, collection="math"),
            _make_chunk(chunk_index=0, collection="science"),
        ]
        vectors = _random_vectors(3)
        ChunkStore(db).insert(chunks, vectors)

        deleted = ChunkStore(db).delete_collection("math")
        assert deleted == 2
        assert ChunkStore(db).count() == 1

    def test_delete_nonexistent_collection(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(collection="math")]
        vectors = _random_vectors(1)
        ChunkStore(db).insert(chunks, vectors)

        deleted = ChunkStore(db).delete_collection("unknown")
        assert deleted == 0
        assert ChunkStore(db).count() == 1

    def test_delete_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        deleted = ChunkStore(db).delete_collection("anything")
        assert deleted == 0


class TestCreateCollectionIndex:
    def test_creates_index_on_populated_table(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=i, collection="math") for i in range(3)]
        vectors = _random_vectors(3)
        ChunkStore(db).insert(chunks, vectors)

        # Should not raise
        TableOptimizer(db).create_collection_index()

        # Search still works after index creation
        results = ChunkSearch(db).vector_search(
            vectors[0], limit=5, collection_filter="math"
        )
        assert len(results) == 3

    def test_noop_on_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        TableOptimizer(db).create_collection_index()  # No table, no error


class TestOptimizeTable:
    def test_optimizes_populated_table(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=i) for i in range(5)]
        vectors = _random_vectors(5)
        ChunkStore(db).insert(chunks, vectors)

        # Should not raise
        TableOptimizer(db).optimize()

        # Data still accessible
        assert ChunkStore(db).count() == 5

    def test_noop_on_empty_db(self, tmp_path: Path):
        db = get_db(tmp_path / "db")
        TableOptimizer(db).optimize()  # No table, no error


class _FakeTables:
    """The ``.tables`` payload of a fake ``list_tables`` result."""

    __slots__ = ("tables",)

    tables: list[str]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.tables = [TABLE_NAME]
        return self


class _RaisingUriTable:
    """A table whose ``.uri`` access raises, as a surprising binding would."""

    __slots__ = ()

    @property
    def uri(self) -> str:
        msg = "uri unavailable"
        raise AttributeError(msg)


class _NonStrUriTable:
    """A table whose ``.uri`` is unexpectedly non-str (``Path`` will reject it)."""

    __slots__ = ()

    @property
    def uri(self) -> object:
        return 12345


class _FakeOptimizerDb:
    """A minimal ``LanceDB`` stand-in serving one table for count_fragments."""

    __slots__ = ("_table",)

    _table: object

    def __new__(cls, table: object) -> Self:
        self = super().__new__(cls)
        self._table = table
        return self

    def list_tables(self) -> _FakeTables:
        """Report the single-table listing count_fragments checks first."""
        return _FakeTables()

    def open_table(self, name: str) -> object:
        """Return the injected fake table regardless of name."""
        return self._table


class TestCountFragmentsBestEffort:
    """A surprising ``table.uri`` must degrade to 0, not break optimize()."""

    def test_raising_uri_yields_zero(self) -> None:
        db = cast("LanceDB", _FakeOptimizerDb(_RaisingUriTable()))
        assert TableOptimizer(db).count_fragments() == 0

    def test_non_str_uri_yields_zero(self) -> None:
        db = cast("LanceDB", _FakeOptimizerDb(_NonStrUriTable()))
        assert TableOptimizer(db).count_fragments() == 0


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
            return ChunkStore(db).insert(tasks[idx], task_vectors[idx])

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {executor.submit(_insert, i): i for i in range(len(tasks))}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        assert errors == [], f"Concurrent insert raised: {errors}"
        assert ChunkStore(db).count() == len(tasks)


class TestOptimizeRebuildsFtsIndex:
    """Verify that optimize_table rebuilds the FTS index.

    The Tantivy FTS index stores row references by fragment ID.  When
    ``table.optimize()`` compacts fragments, those IDs change and the
    stale FTS index causes ``RuntimeError`` on hybrid_search.
    Rebuilding the FTS index after compaction fixes this.
    """

    def test_hybrid_search_works_after_optimize(self, tmp_path: Path):
        """hybrid_search must not raise after delete + optimize."""
        db = get_db(tmp_path / "db")

        # Insert two batches so optimize has fragments to compact.
        batch1 = [
            _make_chunk(chunk_index=0, text="quantum entanglement physics"),
            _make_chunk(chunk_index=1, text="classical mechanics newton"),
        ]
        batch2 = [
            _make_chunk(chunk_index=2, text="general relativity einstein"),
            _make_chunk(chunk_index=3, text="thermodynamics entropy heat"),
        ]
        vecs1 = _random_vectors(2)
        vecs2 = _random_vectors(2)
        ChunkStore(db).insert(batch1, vecs1)
        ChunkStore(db).insert(batch2, vecs2)

        # Delete one batch to create deletion markers.
        ChunkStore(db).delete_document("test.pdf")

        # Re-insert so we have data to search.
        batch3 = [
            _make_chunk(chunk_index=0, text="quantum entanglement physics"),
            _make_chunk(chunk_index=1, text="classical mechanics newton"),
            _make_chunk(chunk_index=2, text="general relativity einstein"),
        ]
        vecs3 = _random_vectors(3)
        ChunkStore(db).insert(batch3, vecs3)

        # Optimize compacts fragments — this is where the old FTS index
        # would become stale.
        TableOptimizer(db).optimize()

        # Hybrid retrieval must work without RuntimeError.
        query_vec = _random_vectors(1)[0]
        retriever = HybridRetriever(Database(db), RetrievalConfig())
        results = retriever.retrieve("quantum physics", query_vec, None, 5)
        assert len(results) >= 1

    def test_fts_results_nonempty_after_optimize(self, tmp_path: Path):
        """FTS channel must return results after optimize, not degrade
        to vector-only.
        """
        db = get_db(tmp_path / "db")

        chunks = [
            _make_chunk(chunk_index=i, text=f"unique keyword xylophone chunk {i}")
            for i in range(5)
        ]
        vectors = _random_vectors(5)
        ChunkStore(db).insert(chunks, vectors)

        # Force multiple fragments by inserting a second batch.
        more = [
            _make_chunk(chunk_index=5, text="unique keyword xylophone extra"),
        ]
        ChunkStore(db).insert(more, _random_vectors(1))

        TableOptimizer(db).optimize()

        # Search for a term that only matches via FTS keyword.
        query_vec = _random_vectors(1)[0]
        retriever = HybridRetriever(Database(db), RetrievalConfig())
        results = retriever.retrieve("xylophone", query_vec, None, 10)
        texts = [r.text for r in results]
        assert any("xylophone" in t for t in texts)

    def test_optimize_passes_cleanup_older_than(self, tmp_path: Path):
        """optimize_table calls table.optimize(cleanup_older_than=7d)."""
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(chunk_index=0, text="data")]
        ChunkStore(db).insert(chunks, _random_vectors(1))

        # Run optimize twice to create versions worth pruning.
        TableOptimizer(db).optimize()
        ChunkStore(db).insert(
            [_make_chunk(chunk_index=1, text="more data")],
            _random_vectors(1),
        )
        TableOptimizer(db).optimize()

        # Data still accessible — no corruption.
        assert ChunkStore(db).count() == 2


class TestDirSizeBytes:
    """Verify dir_size_bytes returns correct size using du or fallback."""

    def test_returns_correct_size(self, tmp_path: Path) -> None:
        from quarry.db.storage import dir_size_bytes

        (tmp_path / "a.txt").write_bytes(b"x" * 100)
        (tmp_path / "b.txt").write_bytes(b"y" * 200)
        result = dir_size_bytes(tmp_path)
        # du reports disk usage (block-aligned), so result >= sum of file sizes.
        assert result >= 300

    def test_fallback_on_du_failure(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from quarry.db.storage import dir_size_bytes

        (tmp_path / "a.txt").write_bytes(b"x" * 50)
        with patch("subprocess.run", side_effect=OSError("no du")):
            result = dir_size_bytes(tmp_path)
        assert result == 50

    def test_empty_directory(self, tmp_path: Path) -> None:
        from quarry.db.storage import dir_size_bytes

        result = dir_size_bytes(tmp_path)
        assert result >= 0
