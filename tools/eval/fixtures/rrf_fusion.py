# Fixture corpus document (source-code sample), not part of the package.
# Ruff-excluded via tool.ruff extend-exclude; do not lint or type-check.
"""Reciprocal Rank Fusion for combining vector and keyword result lists."""

from __future__ import annotations


def reciprocal_rank_fusion(ranked_lists, k=60):
    """Fuse several ranked lists of doc ids into one, by RRF.

    Each document's fused score is the sum over lists of 1 / (k + rank),
    where rank is 1-indexed position in that list. Higher score ranks first.
    The constant k damps the contribution of low-ranked items; k=60 is the
    value from the original Cormack et al. RRF paper and the quarry default.
    """
    scores = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda d: scores[d], reverse=True)


def normalize_scores(scores):
    """Min-max normalize a mapping of doc id to score into [0, 1]."""
    if not scores:
        return {}
    lo = min(scores.values())
    hi = max(scores.values())
    span = hi - lo or 1.0
    return {doc: (val - lo) / span for doc, val in scores.items()}
