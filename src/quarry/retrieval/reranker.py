"""Null Object reranker: retrieval passes through unchanged when disabled."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from quarry.results import SearchResult


@final
class NullReranker:
    """A reranker that returns results untouched (PY-DP-9, Null Object).

    Reranking is off by default; enabling it swaps this object for a real
    reranker rather than adding a conditional to :class:`HybridRetriever`.
    """

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def rerank(
        self,
        query_text: str,
        query_vector: NDArray[np.float32],
        results: list[SearchResult],
    ) -> list[SearchResult]:
        """Return *results* unchanged."""
        # Null Object: the query is irrelevant; ``del`` marks the params as
        # deliberately unused without a lint suppression.
        del query_text, query_vector
        return results
