"""Retrieval seam: the shipped hybrid retriever, shared by production and eval."""

from __future__ import annotations

from quarry.retrieval.config import RetrievalConfig as RetrievalConfig
from quarry.retrieval.fusion import RrfFusion as RrfFusion
from quarry.retrieval.hybrid import HybridRetriever as HybridRetriever
from quarry.retrieval.protocols import Reranker as Reranker, Retriever as Retriever
from quarry.retrieval.reranker import NullReranker as NullReranker
from quarry.retrieval.service import SearchService as SearchService

__all__ = [
    "HybridRetriever",
    "NullReranker",
    "Reranker",
    "RetrievalConfig",
    "Retriever",
    "RrfFusion",
    "SearchService",
]
