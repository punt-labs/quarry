"""Structural interfaces for the retrieval seam: Retriever and Reranker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from quarry.results import SearchFilter, SearchResult


@runtime_checkable
class Reranker(Protocol):
    """Reorder fused results by relevance (PY-DP-11, single method).

    Turning reranking off is a swap for :class:`NullReranker`, never a branch
    inside the retriever body.
    """

    def rerank(
        self,
        query_text: str,
        query_vector: NDArray[np.float32],
        results: list[SearchResult],
    ) -> list[SearchResult]:
        """Return *results* in a possibly-reordered relevance order."""
        ...


@runtime_checkable
class Retriever(Protocol):
    """Produce ranked search results for a query (PY-DP-11, single method).

    The shipped path and the eval runner call the identical object, so the
    committed baseline measures the retriever that actually ships.
    """

    def retrieve(
        self,
        query_text: str,
        query_vector: NDArray[np.float32],
        search_filter: SearchFilter | None,
        limit: int,
    ) -> list[SearchResult]:
        """Return up to *limit* ranked results for the query."""
        ...
