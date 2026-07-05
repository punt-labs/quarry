"""HybridRetriever: the shipped retriever, exercised through a real Database."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import pytest

from quarry.db import ChunkStore, Database, get_db
from quarry.models import Chunk
from quarry.results import SearchFilter
from quarry.retrieval import HybridRetriever, RetrievalConfig, Retriever

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
    """Return a 768-d unit vector whose leading coordinates are *values*."""
    vec = np.zeros(768, dtype=np.float32)
    vec[: len(values)] = values
    unit: NDArray[np.float32] = (vec / np.linalg.norm(vec)).astype(np.float32)
    return unit


def _retriever(db: LanceDB, config: RetrievalConfig | None = None) -> HybridRetriever:
    return HybridRetriever(Database(db), config or RetrievalConfig())


class TestRetrieve:
    def test_empty_table_returns_empty(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        results = _retriever(db).retrieve("anything", _unit([1.0]), None, 5)
        assert results == []

    def test_similarity_is_bounded(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        vec = _unit([1.0, 0.0, 0.0])
        ChunkStore(db).insert([_make_chunk(0, "alpha bravo")], np.stack([vec]))

        results = _retriever(db).retrieve("alpha bravo", vec, None, 5)
        assert len(results) >= 1
        for r in results:
            assert -1.0 - 1e-5 <= (1 - float(r.distance)) <= 1.0 + 1e-5

    def test_exact_search_matches_default_on_small_corpus(self, tmp_path: Path) -> None:
        """Forcing the flat scan yields the same results on an unindexed corpus."""
        db = get_db(tmp_path / "db")
        vec = _unit([1.0, 0.0, 0.0])
        ChunkStore(db).insert([_make_chunk(0, "alpha bravo")], np.stack([vec]))

        default = _retriever(db).retrieve("alpha bravo", vec, None, 5)
        exact = _retriever(db, RetrievalConfig(exact_search=True)).retrieve(
            "alpha bravo", vec, None, 5
        )
        assert [r.to_dict() for r in exact] == [r.to_dict() for r in default]

    def test_filter_scopes_results(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        vec = _unit([1.0, 0.0, 0.0])
        ChunkStore(db).insert([_make_chunk(0, "alpha bravo")], np.stack([vec]))

        scoped = _retriever(db).retrieve(
            "alpha bravo", vec, SearchFilter(collection="missing"), 5
        )
        assert scoped == []

    def test_conforms_to_retriever_protocol(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        assert isinstance(_retriever(db), Retriever)

    def test_fts_only_row_shows_true_cosine(self, tmp_path: Path) -> None:
        """A keyword hit outside the vector top-N reports its real cosine."""
        db = get_db(tmp_path / "db")
        query = _unit([1.0, 0.0, 0.0])
        # Six fillers sit almost on the query (cosine ~1) with no rare keyword,
        # so they saturate the vector channel's top-N (fetch_limit = 2 * 3 = 6).
        fillers = [_unit([1.0, 0.01 * (i + 1), 0.0]) for i in range(6)]
        filler_chunks = [_make_chunk(i, "alpha common") for i in range(6)]
        # The target is orthogonal to the query (cosine ~0) and is the only
        # chunk carrying the keyword: it matches ONLY the FTS channel.
        target = _unit([0.0, 1.0, 0.0])
        target_chunk = _make_chunk(6, "zorpzorp keyword")
        ChunkStore(db).insert(
            [*filler_chunks, target_chunk], np.stack([*fillers, target])
        )

        results = _retriever(db).retrieve("zorpzorp", query, None, 2)
        fts_only = [r for r in results if "zorpzorp" in r.text]
        assert len(fts_only) == 1
        similarity = 1 - float(fts_only[0].distance)
        assert similarity == pytest.approx(float(np.dot(query, target)), abs=1e-4)
        assert similarity < 0.5


class TestAnnotateFtsDistances:
    """FTS-only rows report their true cosine, never a fake 1.00 (quarry-gcnf)."""

    def _annotate(
        self,
        db: LanceDB,
        query: NDArray[np.float32],
        rows: list[dict[str, object]],
    ) -> None:
        _retriever(db)._annotate_fts_distances(query, rows)

    def _similarity(self, row: dict[str, object]) -> float:
        return 1 - float(str(row["_distance"]))

    def test_true_cosine_not_one(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        query = _unit([1.0, 0.0, 0.0])
        stored = _unit([1.0, 1.0, 0.0])  # 45 degrees from the query
        rows: list[dict[str, object]] = [{"vector": stored.tolist()}]

        self._annotate(db, query, rows)

        assert self._similarity(rows[0]) == pytest.approx(0.70710677, abs=1e-5)
        assert self._similarity(rows[0]) < 0.9

    def test_orthogonal_scores_near_zero(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        query = _unit([1.0, 0.0, 0.0])
        rows: list[dict[str, object]] = [{"vector": _unit([0.0, 1.0, 0.0]).tolist()}]

        self._annotate(db, query, rows)

        assert self._similarity(rows[0]) == pytest.approx(0.0, abs=1e-6)

    def test_missing_vector_is_worst_case(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        query = _unit([1.0, 0.0, 0.0])
        rows: list[dict[str, object]] = [{"text": "keyword hit, no vector"}]

        self._annotate(db, query, rows)

        assert rows[0]["_distance"] == pytest.approx(2.0)
        assert self._similarity(rows[0]) == pytest.approx(-1.0)

    def test_zero_norm_vector_is_worst_case(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        query = _unit([1.0, 0.0, 0.0])
        rows: list[dict[str, object]] = [{"vector": [0.0] * 768}]

        self._annotate(db, query, rows)

        assert self._similarity(rows[0]) == pytest.approx(-1.0)

    def test_zero_norm_query_is_worst_case(self, tmp_path: Path) -> None:
        """A zero-length query has no direction: every row is worst-case."""
        db = get_db(tmp_path / "db")
        query = np.zeros(768, dtype=np.float32)  # degenerate: norm 0, no direction
        rows: list[dict[str, object]] = [{"vector": _unit([1.0, 0.0, 0.0]).tolist()}]

        self._annotate(db, query, rows)

        assert rows[0]["_distance"] == pytest.approx(2.0)
        assert self._similarity(rows[0]) == pytest.approx(-1.0)
