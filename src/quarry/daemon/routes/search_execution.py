"""Execute and observe one validated daemon search."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Self, final

from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from quarry.recall_telemetry import SearchObservation
from quarry.results import SearchFilter
from quarry.retrieval import SearchService
from quarry.retrieval.config import RetrievalConfig

if TYPE_CHECKING:
    from starlette.datastructures import QueryParams

    from quarry.daemon.context import DaemonContext
    from quarry.results import SearchResult


logger = logging.getLogger(__name__)


@final
class SearchExecution:
    """Run a single search over the daemon's resources and prepare the response."""

    __slots__ = ("_ctx", "_params")

    _ctx: DaemonContext
    _params: QueryParams

    def __new__(cls, ctx: DaemonContext, params: QueryParams) -> Self:
        """Bind the daemon and validated request parameters for one search."""
        self = super().__new__(cls)
        self._ctx = ctx
        self._params = params
        return self

    def respond(self, query: str) -> JSONResponse:
        """Return ranked hits and schedule post-response usage capture."""
        limit = self._limit()
        search_filter = self._filter()
        started = time.perf_counter()
        results = self._execute(query, search_filter, limit)
        observation = SearchObservation(
            query=query,
            surface=self._params.get("surface", ""),
            agent=search_filter.agent_handle or "",
            latency_ms=(time.perf_counter() - started) * 1000,
            results=results,
        )
        formatted = [result.to_dict() for result in results]
        logger.debug("Search results=%d", len(formatted))
        return JSONResponse(
            {"query": query, "total_results": len(formatted), "results": formatted},
            background=self._background(observation),
        )

    def _execute(
        self, query: str, search_filter: SearchFilter, limit: int
    ) -> list[SearchResult]:
        query_vector = self._ctx.embedder.embed_query(query)
        config = RetrievalConfig(
            decay_rate=self._ctx.settings.retrieval_decay_rate,
            lesson_boost=self._ctx.settings.retrieval_lesson_boost,
        )
        return SearchService(self._ctx.query_database, config).search(
            query, query_vector, search_filter, limit
        )

    def _background(self, observation: SearchObservation) -> BackgroundTask | None:
        if not self._ctx.recall.enabled:
            return None
        return BackgroundTask(self._ctx.recall.record, observation)

    def _limit(self) -> int:
        """Clamp the ``limit`` query param to ``[1, 50]`` (default 10)."""
        try:
            return max(1, min(int(self._params.get("limit", "10")), 50))
        except ValueError:
            return 10

    def _filter(self) -> SearchFilter:
        """Build a :class:`SearchFilter` from the optional query params."""
        return SearchFilter(
            collection=self._params.get("collection") or None,
            document=self._params.get("document") or None,
            page_type=self._params.get("page_type") or None,
            source_format=self._params.get("source_format") or None,
            agent_handle=self._params.get("agent_handle") or None,
            memory_type=self._params.get("memory_type") or None,
        )
