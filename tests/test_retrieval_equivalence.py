"""Characterization guard: HybridRetriever's output is frozen to the shipped path.

The Phase-0 equivalence gate proved (in the commit that introduced this file)
that ``HybridRetriever`` with the default ``RetrievalConfig`` returns
byte-identical results to the legacy ``ChunkSearch.hybrid_search`` — same
ordering AND same similarity scores — across FTS-only, vector+FTS-overlap, and
filtered scenarios, including both-filters-set (proving the SearchFilter
predicate order is result-neutral under AND-commutativity).

The legacy code is now deleted, so this guard freezes that proven output as a
golden snapshot. ``GOLDEN`` was captured from the retriever at the moment it was
verified equal to the legacy path; a drift here means the shipped retriever
changed and the eval baseline would no longer measure what ships.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import pytest

from quarry.db import ChunkStore, Database, get_db
from quarry.models import Chunk
from quarry.results import SearchFilter, SearchResult
from quarry.retrieval import HybridRetriever, RetrievalConfig

if TYPE_CHECKING:
    from pathlib import Path

    from numpy.typing import NDArray

    from quarry.types import LanceDB

# (document_name, chunk_index, page_number, similarity) for query "alpha rare
# beta", limit 8, no filter over the fixed corpus below. RRF-ordered rows with
# cosine-valued similarity -- the documented "RRF order, cosine score" shape.
GOLDEN: list[tuple[str, int, int, float]] = [
    ("b.md", 2, 3, 0.9578),
    ("a.md", 1, 2, 0.9988),
    ("c.md", 3, 4, 0.995),
    ("a.md", 0, 1, 0.9998),
    ("d.md", 5, 6, 0.0),
    ("a.md", 7, 8, 0.9968),
    ("c.md", 4, 5, 0.9889),
    ("b.md", 6, 7, 0.0),
]


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


def _retrieve(
    db: LanceDB,
    query_text: str,
    vec: NDArray[np.float32],
    limit: int,
    sf: SearchFilter | None,
) -> list[SearchResult]:
    retriever = HybridRetriever(Database(db), RetrievalConfig())
    return retriever.retrieve(query_text, vec, sf, limit)


class TestShippedRetrieverIsFrozen:
    """The default HybridRetriever reproduces the proven legacy output."""

    def test_matches_golden_ordering_and_scores(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        query = _corpus(db)
        results = _retrieve(db, "alpha rare beta", query, 8, None)
        actual = [
            (r.document_name, r.chunk_index, r.page_number, r.similarity)
            for r in results
        ]
        assert actual == GOLDEN

    def test_collection_filter_scopes_to_that_collection(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "db")
        query = _corpus(db)
        results = _retrieve(
            db, "alpha common", query, 10, SearchFilter(collection="notes")
        )
        assert {r.document_name for r in results} <= {"c.md", "d.md"}
        assert results  # the filter matches at least one chunk

    def test_both_filters_set_is_result_neutral(self, tmp_path: Path) -> None:
        """collection + document filters: only a.md chunks in docs come back."""
        db = get_db(tmp_path / "db")
        query = _corpus(db)
        results = _retrieve(
            db,
            "alpha common",
            query,
            10,
            SearchFilter(collection="docs", document="a.md"),
        )
        assert {r.document_name for r in results} == {"a.md"}

    def test_fts_only_row_reports_true_cosine(self, tmp_path: Path) -> None:
        """The FTS-only 'zorpzorp' row reports its real cosine on the golden corpus.

        limit=2 -> fetch_limit=6, so the six alpha rows saturate the vector
        channel and push the orthogonal 'zorpzorp' row OUT of the vector
        top-N. Its distance can then come only from the annotate path
        (quarry-gcnf), not the vector channel -- so this assertion is
        load-bearing on ``_annotate_fts_distances``. With limit=3 (fetch=9 >
        8-row corpus) the vector channel returned the whole corpus and the
        annotate path was never exercised.
        """
        db = get_db(tmp_path / "db")
        query = _corpus(db)
        results = _retrieve(db, "zorpzorp", query, 2, None)
        fts_only = [r for r in results if "zorpzorp" in r.text]
        assert len(fts_only) == 1
        # Orthogonal to the query: true cosine 0.0, never the fake 1.00 a
        # missing annotation would show. A no-op annotate defaults the row to
        # WORST_CASE_DISTANCE (similarity -1.0), failing this exact assertion.
        assert (1 - fts_only[0].distance) == pytest.approx(0.0, abs=1e-4)
