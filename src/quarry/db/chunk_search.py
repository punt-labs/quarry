"""Chunk search by vector similarity under the cosine metric.

Hybrid BM25+vector retrieval and RRF fusion moved to ``quarry.retrieval``
(the production seam shared with the eval harness); this module now owns only
single-channel vector search.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

from quarry._sql import escape_sql
from quarry.db.schema import TABLE_NAME
from quarry.results import SearchResult
from quarry.types import LanceDB

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)


class ChunkSearch:
    """Search chunks by vector similarity under the cosine metric."""

    __slots__ = ("_db",)

    _db: LanceDB

    def __new__(cls, db: LanceDB) -> Self:
        self = super().__new__(cls)
        self._db = db
        return self

    def vector_search(
        self,
        query_vector: NDArray[np.float32],
        limit: int = 10,
        document_filter: str | None = None,
        collection_filter: str | None = None,
        page_type_filter: str | None = None,
        source_format_filter: str | None = None,
    ) -> list[SearchResult]:
        """Search for similar chunks using vector similarity."""
        logger.debug(
            "Search: limit=%d, document_filter=%s, collection_filter=%s, "
            "page_type_filter=%s, source_format_filter=%s",
            limit,
            document_filter,
            collection_filter,
            page_type_filter,
            source_format_filter,
        )

        if TABLE_NAME not in self._db.list_tables().tables:
            logger.debug("Search: table %s not found, returning empty", TABLE_NAME)
            return []

        table = self._db.open_table(TABLE_NAME)
        # Cosine metric on unit-length vectors makes ``_distance = 1 - cos(θ)``
        # so ``similarity = 1 - _distance`` is true cosine ∈ [-1, 1] (quarry-3a7f).
        query = table.search(query_vector.tolist()).metric("cosine").limit(limit)

        predicates: list[str] = []
        if document_filter:
            predicates.append(f"document_name = '{escape_sql(document_filter)}'")
        if collection_filter:
            predicates.append(f"collection = '{escape_sql(collection_filter)}'")
        if page_type_filter:
            predicates.append(f"page_type = '{escape_sql(page_type_filter)}'")
        if source_format_filter:
            predicates.append(f"source_format = '{escape_sql(source_format_filter)}'")
        if predicates:
            query = query.where(" AND ".join(predicates))

        results = query.to_list()
        logger.debug("Search: %d results returned", len(results))
        return [SearchResult.from_row(r) for r in results]
