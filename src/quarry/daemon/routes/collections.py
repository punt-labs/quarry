"""The ``/collections`` routes: list collections and delete one."""

from __future__ import annotations

from functools import partial
from typing import final

from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.daemon.routes.base import RouteGroup


@final
class CollectionRoutes(RouteGroup):
    """Serve ``GET /collections`` (list) and ``DELETE /collections`` (async purge)."""

    def collections(self, request: Request) -> JSONResponse:
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp

        cols = self.ctx.database.catalog.list_collections()
        return JSONResponse({"total_collections": len(cols), "collections": cols})

    async def delete(self, request: Request) -> JSONResponse:
        """Handle DELETE /collections as an async 202 background task."""
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp

        name = request.query_params.get("name", "")
        if not name:
            return JSONResponse(
                {"error": "Missing required parameter: name"}, status_code=400
            )

        state = self.ctx.tasks.begin("delete")
        delete_call = partial(self.ctx.database.store.delete_collection, name)
        return self.accept(
            state, self.run_delete(state, delete_call, name, "collection")
        )
