"""NullReranker is the identity reranker: results pass through untouched."""

from __future__ import annotations

import numpy as np

from quarry.results import SearchResult
from quarry.retrieval import NullReranker, Reranker


def _result(name: str) -> SearchResult:
    return SearchResult.from_row({"document_name": name, "_distance": 0.1})


class TestNullReranker:
    def test_returns_results_unchanged(self) -> None:
        results = [_result("a"), _result("b")]
        vec = np.zeros(768, dtype=np.float32)
        assert NullReranker().rerank("query", vec, results) is results

    def test_preserves_order(self) -> None:
        results = [_result("b"), _result("a")]
        vec = np.zeros(768, dtype=np.float32)
        out = NullReranker().rerank("query", vec, results)
        assert [r.document_name for r in out] == ["b", "a"]

    def test_satisfies_reranker_protocol(self) -> None:
        assert isinstance(NullReranker(), Reranker)

    def test_has_no_dict(self) -> None:
        assert not hasattr(NullReranker(), "__dict__")
