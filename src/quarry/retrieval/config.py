"""RetrievalConfig: the knobs that make retrieval reproducible and A/B-able."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from quarry.retrieval.reranker import NullReranker

if TYPE_CHECKING:
    from quarry.retrieval.protocols import Reranker

# Production defaults -- every value below reproduces today's shipped hybrid
# search exactly, so ``RetrievalConfig()`` is the committed baseline.
_DEFAULT_RRF_K = 60
_DEFAULT_FETCH_MULTIPLIER = 3
_DEFAULT_METRIC = "cosine"
_DEFAULT_EMBEDDING_STRATEGY = "baseline"


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    """Frozen bundle of retrieval knobs (PY-CC-6).

    Defaults reproduce today's production hybrid search bit-for-bit: RRF
    ``k=60``, ``3x`` over-fetch, cosine metric, ANN vector search (exact search
    *not* forced), no reranking, the baseline embedding, and no temporal decay.

    ``exact_search`` carries the determinism contract's flat-vs-ANN choice
    through the seam: the eval runner sets it to force LanceDB's exact scan on a
    small fixture (ANN is seed-dependent) rather than monkeypatching the query.
    ``decay_rate`` carries the agent-memory temporal-decay knob that
    ``hybrid_search`` exposed; production leaves it at ``0.0`` (no decay).
    """

    rrf_k: int = _DEFAULT_RRF_K
    fetch_multiplier: int = _DEFAULT_FETCH_MULTIPLIER
    metric: str = _DEFAULT_METRIC
    exact_search: bool = False
    reranker: Reranker = field(default_factory=NullReranker)
    embedding_strategy: str = _DEFAULT_EMBEDDING_STRATEGY
    decay_rate: float = 0.0
