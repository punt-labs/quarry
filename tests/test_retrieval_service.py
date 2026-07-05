"""SearchService: the shared production pipeline every surface calls."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np

from quarry.db import Database, get_db
from quarry.models import Chunk
from quarry.retrieval import HybridRetriever, RetrievalConfig, SearchService

if TYPE_CHECKING:
    from pathlib import Path

    from numpy.typing import NDArray

    from quarry.types import LanceDB


def _make_chunk(chunk_index: int, text: str) -> Chunk:
    return Chunk(
        document_name="test.pdf",
        document_path="/tmp/test.pdf",
        collection="default",
        page_number=1,
        total_pages=1,
        chunk_index=chunk_index,
        text=text,
        page_raw_text=text,
        page_type="text",
        source_format=".pdf",
        ingestion_timestamp=datetime.now(tz=UTC),
    )


def _unit(values: list[float]) -> NDArray[np.float32]:
    vec = np.zeros(768, dtype=np.float32)
    vec[: len(values)] = values
    unit: NDArray[np.float32] = (vec / np.linalg.norm(vec)).astype(np.float32)
    return unit


def _seed(db: LanceDB) -> NDArray[np.float32]:
    from quarry.db import ChunkStore

    vec = _unit([1.0, 0.0, 0.0])
    ChunkStore(db).insert([_make_chunk(0, "alpha bravo")], np.stack([vec]))
    return vec


class TestSearchService:
    def test_empty_table_returns_empty(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        results = SearchService(Database(db)).search("x", _unit([1.0]), None, 5)
        assert results == []

    def test_returns_ranked_results(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        vec = _seed(db)
        results = SearchService(Database(db)).search("alpha bravo", vec, None, 5)
        assert len(results) >= 1
        assert results[0].document_name == "test.pdf"

    def test_matches_direct_hybrid_retriever(self, tmp_path: Path) -> None:
        """The service is a thin default-config wrapper over the retriever."""
        db = get_db(tmp_path / "db")
        vec = _seed(db)
        via_service = SearchService(Database(db)).search("alpha bravo", vec, None, 5)
        direct = HybridRetriever(Database(db), RetrievalConfig()).retrieve(
            "alpha bravo", vec, None, 5
        )
        assert [r.to_dict() for r in via_service] == [r.to_dict() for r in direct]

    def test_custom_config_is_honored(self, tmp_path: Path) -> None:
        """A passed config threads through to the retriever (exact_search here)."""
        db = get_db(tmp_path / "db")
        vec = _seed(db)
        service = SearchService(Database(db), RetrievalConfig(exact_search=True))
        results = service.search("alpha bravo", vec, None, 5)
        default = SearchService(Database(db)).search("alpha bravo", vec, None, 5)
        assert [r.to_dict() for r in results] == [r.to_dict() for r in default]
