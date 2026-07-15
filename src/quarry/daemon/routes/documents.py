"""The ``/documents`` routes: list indexed documents and delete one."""

from __future__ import annotations

from functools import partial
from typing import final

from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.daemon.routes.base import RouteGroup


@final
class DocumentRoutes(RouteGroup):
    """Serve ``GET /documents`` (list) and ``DELETE /documents`` (async purge)."""

    def documents(self, request: Request) -> JSONResponse:
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp

        collection = request.query_params.get("collection") or None
        docs = self.ctx.database.catalog.list_documents(collection_filter=collection)
        return JSONResponse({"total_documents": len(docs), "documents": docs})

    async def delete(self, request: Request) -> JSONResponse:
        """Handle DELETE /documents as an async 202 background task."""
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp

        name = request.query_params.get("name", "")
        if not name:
            return JSONResponse(
                {"error": "Missing required parameter: name"}, status_code=400
            )

        collection = request.query_params.get("collection") or None
        state = self.ctx.tasks.begin("delete")
        delete_call = partial(
            self.ctx.database.store.delete_document, name, collection=collection
        )
        return self.accept(state, self.run_delete(state, delete_call, name, "document"))
