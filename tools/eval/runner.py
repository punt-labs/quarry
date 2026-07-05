"""EvalRunner: index the fixture under a RetrievalConfig, run queries, emit a run.

The page-collapse (best-ranked chunk per JudgedUnit) happens here, *after* the
shared ``HybridRetriever`` returns — never inside the retriever, which keeps
returning per-chunk results for production. The authoritative ranking is the
retriever's returned list order (its RRF fusion), not a per-chunk cosine, so
collapse keys on list position, giving a deterministic, tie-free order that is
then re-sorted with an explicit JudgedUnit tie-break (the determinism contract).

Indexing and collapse themselves live in ``indexing.py``; this module owns only
the run: driving the query set through the seam and packaging ``RunOutput``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from quarry.retrieval import RetrievalConfig, SearchService
from tools.eval.indexing import Collapser, EphemeralIndex
from tools.eval.provenance import Determinism
from tools.eval.trec import Qrels, TrecRun

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.config import Settings
    from quarry.results import SearchResult
    from tools.eval.corpus import Corpus
    from tools.eval.queryset import QuerySet

# Fetch enough per-chunk hits that page-collapse still leaves >= 10 units for
# success@10, and that pollution@10 sees a full top-10 of chunks.
_RETRIEVE_LIMIT = 60


@dataclass(frozen=True, slots=True)
class RunOutput:
    """The runner's product: the page-keyed run, its qrels, and the raw chunks.

    ``chunk_results`` (per-query, per-chunk, pre-collapse) feeds the pollution
    diagnostic, which classifies chunks, not collapsed units.
    """

    run: TrecRun
    qrels: Qrels
    chunk_results: dict[str, list[SearchResult]]


class EvalRunner:
    """Index the fixture, run the query set through the seam, emit a run."""

    __slots__ = ("_corpus", "_queryset", "_settings", "_workdir")

    _corpus: Corpus
    _queryset: QuerySet
    _settings: Settings
    _workdir: Path

    def __new__(
        cls, corpus: Corpus, queryset: QuerySet, settings: Settings, workdir: Path
    ) -> Self:
        self = super().__new__(cls)
        self._corpus = corpus
        self._queryset = queryset
        self._settings = settings
        self._workdir = workdir
        return self

    def run(self, config: RetrievalConfig, tag: str) -> RunOutput:
        """Index (or reuse), retrieve every query, and collapse to a page-keyed run."""
        Determinism.apply()
        from quarry.ingestion.backends import get_embedding_backend  # noqa: PLC0415

        index = EphemeralIndex(
            self._workdir, self._corpus, self._settings, config.embedding_strategy
        )
        database = index.database()
        embedder = get_embedding_backend(self._settings)
        service = SearchService(database, config)

        rankings: dict[str, list[tuple[str, float]]] = {}
        chunk_results: dict[str, list[SearchResult]] = {}
        for query in self._queryset:
            vector = embedder.embed_query(query.text)
            results = service.search(query.text, vector, None, _RETRIEVE_LIMIT)
            chunk_results[query.query_id] = results
            page_level = query.answer.is_page_level if query.answer else True
            rankings[query.query_id] = Collapser.collapse(
                results, page_level=page_level
            )

        return RunOutput(
            run=TrecRun(rankings, tag),
            qrels=self._queryset.to_qrels(),
            chunk_results=chunk_results,
        )
