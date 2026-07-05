"""HybridRetriever: the shipped BM25 + vector + RRF retriever, behind a config."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

import numpy as np

from quarry.db.schema import TABLE_NAME
from quarry.results import WORST_CASE_DISTANCE, SearchResult
from quarry.retrieval.fusion import RrfFusion

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from quarry.db.facade import Database
    from quarry.results import SearchFilter
    from quarry.retrieval.config import RetrievalConfig
    from quarry.types import LanceTable

logger = logging.getLogger(__name__)


class HybridRetriever:
    """Multi-channel retrieval: vector similarity + BM25 FTS, fused with RRF.

    Constructed over a :class:`Database` facade and a :class:`RetrievalConfig`.
    The same object serves the shipped CLI/HTTP/MCP paths and the eval runner,
    so the committed baseline and production output cannot drift. With the
    default config the behavior is bit-for-bit the legacy ``hybrid_search``.
    """

    __slots__ = ("_config", "_database", "_fusion")

    _database: Database
    _config: RetrievalConfig
    _fusion: RrfFusion

    def __new__(cls, database: Database, config: RetrievalConfig) -> Self:
        self = super().__new__(cls)
        self._database = database
        self._config = config
        self._fusion = RrfFusion(config.rrf_k, config.decay_rate)
        return self

    def retrieve(
        self,
        query_text: str,
        query_vector: NDArray[np.float32],
        search_filter: SearchFilter | None,
        limit: int,
    ) -> list[SearchResult]:
        """Return up to *limit* ranked results, fused across channels and reranked."""
        db = self._database.db
        if TABLE_NAME not in db.list_tables().tables:
            return []

        table = db.open_table(TABLE_NAME)
        self._database.schema.migrate(table)

        predicate = search_filter.to_predicate() if search_filter is not None else None
        fetch_limit = limit * self._config.fetch_multiplier

        vec_results = self._vector_channel(table, query_vector, predicate, fetch_limit)
        fts_results = self._fts_channel(table, query_text, predicate, fetch_limit)
        self._annotate_fts_distances(query_vector, fts_results)

        fused = self._fusion.fuse(vec_results, fts_results, limit)
        return self._config.reranker.rerank(query_text, query_vector, fused)

    def _vector_channel(
        self,
        table: LanceTable,
        query_vector: NDArray[np.float32],
        predicate: str | None,
        fetch_limit: int,
    ) -> list[dict[str, object]]:
        """Run the vector-similarity channel under the configured metric.

        Cosine on unit-length vectors makes ``_distance = 1 - cos(theta)`` so
        ``similarity = 1 - _distance`` is true cosine (quarry-3a7f). When
        ``exact_search`` is set the ANN index is bypassed for a deterministic
        flat scan -- the determinism contract's flat-vs-ANN lever.
        """
        query = (
            table.search(query_vector.tolist())
            .metric(self._config.metric)
            .limit(fetch_limit)
        )
        if self._config.exact_search:
            query = query.bypass_vector_index()
        if predicate:
            query = query.where(predicate)
        return query.to_list()

    def _fts_channel(
        self,
        table: LanceTable,
        query_text: str,
        predicate: str | None,
        fetch_limit: int,
    ) -> list[dict[str, object]]:
        """Run the BM25 channel; empty on legacy tables lacking an FTS index."""
        try:
            fts_query = table.search(query_text, query_type="fts").limit(fetch_limit)
            if predicate:
                fts_query = fts_query.where(predicate)
            return fts_query.to_list()
        except (OSError, ValueError, RuntimeError):
            logger.warning(
                "FTS search failed, using vector-only results", exc_info=True
            )
            return []

    def _annotate_fts_distances(
        self,
        query_vector: NDArray[np.float32],
        fts_rows: list[dict[str, object]],
    ) -> None:
        """Set each FTS row's ``_distance`` to its true cosine distance.

        LanceDB supplies ``_distance`` for the vector channel; FTS rows arrive
        without one. Under the cosine metric ``_distance = 1 - cos(theta)``, so a
        row's displayed ``similarity = 1 - _distance`` is its real cosine. A row
        whose stored vector is missing or zero-length gets the worst-case
        distance ``WORST_CASE_DISTANCE`` (similarity -1), never the fake 0.0 that
        made irrelevant keyword hits display as 1.00 (quarry-gcnf).
        """
        query = np.asarray(query_vector, dtype=np.float32).ravel()
        q_norm = float(np.linalg.norm(query))
        for row in fts_rows:
            raw = row.get("vector")
            if raw is None or q_norm == 0.0:
                row["_distance"] = WORST_CASE_DISTANCE
                continue
            stored = np.asarray(raw, dtype=np.float32).ravel()
            s_norm = float(np.linalg.norm(stored))
            if s_norm == 0.0:
                row["_distance"] = WORST_CASE_DISTANCE
                continue
            cos = float(np.dot(query, stored) / (q_norm * s_norm))
            row["_distance"] = 1.0 - cos
