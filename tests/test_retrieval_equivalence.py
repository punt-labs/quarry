"""Equivalence gate: HybridRetriever must match the legacy hybrid_search exactly.

This is the Phase-0 characterization test. It lands *before* the extraction
deletes ``ChunkSearch.hybrid_search`` and proves the new seam produces
byte-identical results — same ordering AND same similarity scores — so the
future eval baseline measures the retriever that actually ships. Without it a
subtle refactor drift would silently invalidate the regression guard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np

from quarry.db import ChunkSearch, ChunkStore, Database, get_db
from quarry.models import Chunk
from quarry.results import SearchFilter, SearchResult
from quarry.retrieval import HybridRetriever, RetrievalConfig

if TYPE_CHECKING:
    from pathlib import Path

    from numpy.typing import NDArray

    from quarry.types import LanceDB


def _unit(values: list[float]) -> NDArray[np.float32]:
    vec = np.zeros(768, dtype=np.float32)
    vec[: len(values)] = values
    return vec / np.linalg.norm(vec)


def _chunk(idx: int, text: str, *, collection: str, document: str) -> Chunk:
    return Chunk(
        document_name=document,
        document_path=f"/tmp/{document}",
        collection=collection,
        page_number=idx + 1,
        total_pages=8,
        chunk_index=idx,
        text=text,
        page_raw_text=text,
        page_type="text",
        source_format=".md",
        ingestion_timestamp=datetime.now(tz=UTC),
    )


def _corpus(db: LanceDB) -> NDArray[np.float32]:
    """Insert a fixed 8-chunk corpus spanning two collections and documents."""
    query = _unit([1.0, 0.0, 0.0])
    chunks = [
        _chunk(0, "alpha common context", collection="docs", document="a.md"),
        _chunk(1, "alpha common prose", collection="docs", document="a.md"),
        _chunk(2, "alpha rare beta", collection="docs", document="b.md"),
        _chunk(3, "alpha common filler", collection="notes", document="c.md"),
        _chunk(4, "alpha common more", collection="notes", document="c.md"),
        _chunk(5, "beta gamma delta", collection="notes", document="d.md"),
        _chunk(6, "zorpzorp keyword only", collection="docs", document="b.md"),
        _chunk(7, "alpha common tail", collection="docs", document="a.md"),
    ]
    vectors = np.stack(
        [
            _unit([1.0, 0.02, 0.0]),
            _unit([1.0, 0.05, 0.0]),
            _unit([1.0, 0.30, 0.0]),
            _unit([1.0, 0.10, 0.0]),
            _unit([1.0, 0.15, 0.0]),
            _unit([0.0, 1.0, 0.2]),
            _unit([0.0, 1.0, 0.0]),  # zorpzorp: orthogonal, FTS-only
            _unit([1.0, 0.08, 0.0]),
        ]
    )
    ChunkStore(db).insert(chunks, vectors)
    return query


def _legacy(
    db: LanceDB,
    query_text: str,
    vec: NDArray[np.float32],
    limit: int,
    sf: SearchFilter | None,
) -> list[SearchResult]:
    f = sf or SearchFilter()
    return ChunkSearch(db).hybrid_search(
        query_text,
        vec,
        limit=limit,
        document_filter=f.document,
        collection_filter=f.collection,
        page_type_filter=f.page_type,
        source_format_filter=f.source_format,
        agent_handle_filter=f.agent_handle,
        memory_type_filter=f.memory_type,
    )


def _new(
    db: LanceDB,
    query_text: str,
    vec: NDArray[np.float32],
    limit: int,
    sf: SearchFilter | None,
) -> list[SearchResult]:
    retriever = HybridRetriever(Database(db), RetrievalConfig())
    return retriever.retrieve(query_text, vec, sf, limit)


def _rows(results: list[SearchResult]) -> list[dict[str, object]]:
    return [r.to_dict() for r in results]


class TestEquivalence:
    """New retriever == legacy hybrid_search: identical ordering and scores."""

    def test_fts_only_scenario_matches(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        query = _corpus(db)
        old = _legacy(db, "zorpzorp", query, 3, None)
        new = _new(db, "zorpzorp", query, 3, None)
        assert _rows(new) == _rows(old)

    def test_vector_and_fts_overlap_matches(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        query = _corpus(db)
        old = _legacy(db, "alpha common", query, 5, None)
        new = _new(db, "alpha common", query, 5, None)
        assert _rows(new) == _rows(old)

    def test_collection_filter_matches(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        query = _corpus(db)
        sf = SearchFilter(collection="notes")
        old = _legacy(db, "alpha common", query, 10, sf)
        new = _new(db, "alpha common", query, 10, sf)
        assert _rows(new) == _rows(old)

    def test_document_and_collection_filter_matches(self, tmp_path: Path) -> None:
        """Both filters set: SearchFilter's predicate order differs from the
        legacy builder's, but AND is commutative so results are identical."""
        db = get_db(tmp_path / "db")
        query = _corpus(db)
        sf = SearchFilter(collection="docs", document="a.md")
        old = _legacy(db, "alpha common", query, 10, sf)
        new = _new(db, "alpha common", query, 10, sf)
        assert _rows(new) == _rows(old)

    def test_scores_identical_not_just_ordering(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        query = _corpus(db)
        old = _legacy(db, "alpha rare beta", query, 8, None)
        new = _new(db, "alpha rare beta", query, 8, None)
        assert [r.similarity for r in new] == [r.similarity for r in old]
        assert [(r.document_name, r.chunk_index) for r in new] == [
            (r.document_name, r.chunk_index) for r in old
        ]
