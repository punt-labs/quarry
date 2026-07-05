"""RetrievalConfig defaults must reproduce today's production hybrid search."""

from __future__ import annotations

import dataclasses

import pytest

from quarry.retrieval import NullReranker, RetrievalConfig


class TestDefaults:
    """The zero-arg config is the committed production baseline."""

    def test_rrf_k_is_60(self) -> None:
        assert RetrievalConfig().rrf_k == 60

    def test_fetch_multiplier_is_3(self) -> None:
        assert RetrievalConfig().fetch_multiplier == 3

    def test_metric_is_cosine(self) -> None:
        assert RetrievalConfig().metric == "cosine"

    def test_exact_search_off(self) -> None:
        """Production uses ANN (never forces exact); the runner flips this on."""
        assert RetrievalConfig().exact_search is False

    def test_reranker_is_null(self) -> None:
        assert isinstance(RetrievalConfig().reranker, NullReranker)

    def test_embedding_strategy_is_baseline(self) -> None:
        assert RetrievalConfig().embedding_strategy == "baseline"

    def test_decay_rate_is_zero(self) -> None:
        """Production never applies temporal decay."""
        assert RetrievalConfig().decay_rate == 0.0


class TestValueObject:
    def test_is_frozen(self) -> None:
        cfg = RetrievalConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.rrf_k = 10  # type: ignore[misc]

    def test_has_no_dict(self) -> None:
        """slots=True means no per-instance __dict__ overhead."""
        assert not hasattr(RetrievalConfig(), "__dict__")

    def test_each_config_gets_its_own_reranker(self) -> None:
        """default_factory yields a fresh NullReranker, not a shared mutable."""
        assert RetrievalConfig().reranker is not RetrievalConfig().reranker

    def test_overrides_apply(self) -> None:
        cfg = RetrievalConfig(rrf_k=10, fetch_multiplier=5, exact_search=True)
        assert (cfg.rrf_k, cfg.fetch_multiplier, cfg.exact_search) == (10, 5, True)
