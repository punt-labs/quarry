"""The ``/search`` route: hybrid search over the daemon's single database."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, final

from starlette.responses import JSONResponse

from quarry.daemon.routes.base import RouteGroup
from quarry.results import SearchFilter
from quarry.retrieval import SearchService

if TYPE_CHECKING:
    from starlette.datastructures import QueryParams
    from starlette.requests import Request

logger = logging.getLogger(__name__)


@final
class SearchRoutes(RouteGroup):
    """Serve ``GET /search`` — clamp the limit, build the filter, rank hits."""

    def search(self, request: Request) -> JSONResponse:
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp

        params = request.query_params
        query = params.get("q", "")
        if not query:
            return JSONResponse(
                {"error": "Missing required parameter: q"}, status_code=400
            )

        limit = self._limit(params)
        search_filter = self._filter(params)
        query_vector = self.ctx.embedder.embed_query(query)
        results = SearchService(self.ctx.query_database).search(
            query, query_vector, search_filter, limit
        )
        formatted = [r.to_dict() for r in results]

        logger.info("Search results=%d", len(formatted))
        return JSONResponse(
            {"query": query, "total_results": len(formatted), "results": formatted}
        )

    @staticmethod
    def _limit(params: QueryParams) -> int:
        """Clamp the ``limit`` query param to ``[1, 50]`` (default 10)."""
        try:
            return max(1, min(int(params.get("limit", "10")), 50))
        except ValueError:
            return 10

    @staticmethod
    def _filter(params: QueryParams) -> SearchFilter:
        """Build a :class:`SearchFilter` from the optional query params."""
        return SearchFilter(
            collection=params.get("collection") or None,
            document=params.get("document") or None,
            page_type=params.get("page_type") or None,
            source_format=params.get("source_format") or None,
            agent_handle=params.get("agent_handle") or None,
            memory_type=params.get("memory_type") or None,
        )
