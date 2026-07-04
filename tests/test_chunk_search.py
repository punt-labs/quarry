"""Cosine-metric vector search: self-match ~1.0, bounded scores, angular order."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import pytest

from quarry.db import ChunkSearch, ChunkStore, get_db
from quarry.models import Chunk

if TYPE_CHECKING:
    from pathlib import Path

    from numpy.typing import NDArray


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
    return vec / np.linalg.norm(vec)


class TestCosineMetric:
    def test_self_match_similarity_is_one(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        vec = _unit([1.0, 0.0, 0.0])
        ChunkStore(db).insert([_make_chunk(0, "alpha")], np.stack([vec]))

        results = ChunkSearch(db).vector_search(vec, limit=1)
        assert len(results) == 1
        similarity = 1 - float(results[0]["_distance"])
        assert similarity == pytest.approx(1.0, abs=1e-5)

    def test_similarity_bounded(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        stored = _unit([-1.0, 0.0, 0.0])  # opposite direction from the query
        ChunkStore(db).insert([_make_chunk(0, "opposite")], np.stack([stored]))

        query = _unit([1.0, 0.0, 0.0])
        results = ChunkSearch(db).vector_search(query, limit=1)
        similarity = 1 - float(results[0]["_distance"])
        assert -1.0 - 1e-5 <= similarity <= 1.0 + 1e-5
        # Antipodal unit vectors have cosine -1.
        assert similarity == pytest.approx(-1.0, abs=1e-5)

    def test_orthogonal_similarity_near_zero(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        stored = _unit([0.0, 1.0, 0.0])
        ChunkStore(db).insert([_make_chunk(0, "orthogonal")], np.stack([stored]))

        query = _unit([1.0, 0.0, 0.0])
        results = ChunkSearch(db).vector_search(query, limit=1)
        similarity = 1 - float(results[0]["_distance"])
        assert similarity == pytest.approx(0.0, abs=1e-5)

    def test_angular_ordering(self, tmp_path: Path) -> None:
        """Results rank by angular (cosine) similarity, not vector magnitude."""
        db = get_db(tmp_path / "db")
        query = _unit([1.0, 0.0, 0.0])
        # Increasing angle from the query -> decreasing cosine similarity.
        # `far` carries a large magnitude (10x) to prove ranking ignores it:
        # under a dot-product metric the long `far` vector would rank first,
        # but cosine keeps it last because its angle from the query is widest.
        near = _unit([1.0, 0.2, 0.0])
        mid = _unit([1.0, 1.0, 0.0])
        far = _unit([0.2, 1.0, 0.0]) * np.float32(10.0)
        chunks = [
            _make_chunk(0, "near"),
            _make_chunk(1, "mid"),
            _make_chunk(2, "far"),
        ]
        ChunkStore(db).insert(chunks, np.stack([near, mid, far]))

        results = ChunkSearch(db).vector_search(query, limit=3)
        order = [r["text"] for r in results]
        assert order == ["near", "mid", "far"]

    def test_hybrid_search_similarity_bounded(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        vec = _unit([1.0, 0.0, 0.0])
        ChunkStore(db).insert([_make_chunk(0, "alpha bravo")], np.stack([vec]))

        results = ChunkSearch(db).hybrid_search("alpha bravo", vec, limit=5)
        assert len(results) >= 1
        for r in results:
            similarity = 1 - float(r["_distance"])
            assert -1.0 - 1e-5 <= similarity <= 1.0 + 1e-5
