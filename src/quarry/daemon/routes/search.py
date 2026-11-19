"""Authenticate and validate daemon search requests."""

from __future__ import annotations

from typing import final

from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.daemon.routes.base import RouteGroup
from quarry.daemon.routes.search_execution import SearchExecution


@final
class SearchRoutes(RouteGroup):
    """Serve search — clamp the limit, build the filter, rank hits, log telemetry."""

    def search(self, request: Request) -> JSONResponse:
        """Authenticate, validate a query, and return ranked hybrid-search results.

        Missing q returns HTTP 400; unauthorized requests return HTTP 401.
        Telemetry is recorded after the response when enabled.
        """
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp

        params = request.query_params
        query = params.get("q", "")
        if not query:
            return JSONResponse(
                {"error": "Missing required parameter: q"}, status_code=400
            )

        return SearchExecution(self.ctx, params).respond(query)
