"""The production search pipeline shared by every surface (CLI, HTTP, MCP)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from quarry.retrieval.config import RetrievalConfig
from quarry.retrieval.hybrid import HybridRetriever

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from quarry.db.facade import Database
    from quarry.results import SearchFilter, SearchResult


class SearchService:
    """Run the shipped hybrid retrieval pipeline behind a single call.

    Every surface (CLI, HTTP, MCP) constructs a ``SearchService`` and calls
    :meth:`search`, so the surfaces cannot drift into different retrieval paths
    — the exact failure of bug class 3, where the HTTP endpoint once ran
    vector-only search while the CLI ran hybrid. Surfaces build their own
    ``SearchFilter`` from surface-specific inputs and serialize the results in
    their own shape; the retrieval pipeline in between is this one object.
    """

    __slots__ = ("_retriever",)

    _retriever: HybridRetriever

    def __new__(cls, database: Database, config: RetrievalConfig | None = None) -> Self:
        self = super().__new__(cls)
        self._retriever = HybridRetriever(database, config or RetrievalConfig())
        return self

    def search(
        self,
        query_text: str,
        query_vector: NDArray[np.float32],
        search_filter: SearchFilter | None,
        limit: int,
    ) -> list[SearchResult]:
        """Return ranked results for the query under the configured pipeline."""
        return self._retriever.retrieve(
            query_text, query_vector, search_filter=search_filter, limit=limit
        )
