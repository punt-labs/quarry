"""The show route: fetch document metadata or a single page's text."""

from __future__ import annotations

from typing import final

from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.daemon.routes.base import RouteGroup


@final
class ShowRoutes(RouteGroup):
    """Serve document show — page text when ``page >= 1``, else metadata."""

    def show(self, request: Request) -> JSONResponse:
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp

        document = request.query_params.get("document", "")
        if not document:
            return JSONResponse(
                {"error": "Missing required parameter: document"}, status_code=400
            )

        collection = request.query_params.get("collection") or None
        page = self._page(request.query_params.get("page"))
        if isinstance(page, JSONResponse):
            return page

        if page > 0:
            return self._page_text(document, page, collection)
        return self._metadata(document, collection)

    @staticmethod
    def _page(page_str: str | None) -> int | JSONResponse:
        """Return the requested page (>=1), 0 when ``page`` is omitted, else a 400."""
        if not page_str:
            return 0
        try:
            page = int(page_str)
        except ValueError:
            return JSONResponse(
                {"error": f"Invalid page number: {page_str!r}"}, status_code=400
            )
        if page < 1:
            return JSONResponse(
                {"error": f"Invalid page number: {page} (must be >= 1)"},
                status_code=400,
            )
        return page

    def _page_text(
        self, document: str, page: int, collection: str | None
    ) -> JSONResponse:
        text = self.ctx.database.catalog.get_page_text(
            document, page, collection=collection
        )
        if text is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse(
            {"document_name": document, "page_number": page, "text": text}
        )

    def _metadata(self, document: str, collection: str | None) -> JSONResponse:
        docs = self.ctx.database.catalog.list_documents(collection_filter=collection)
        match = [d for d in docs if d["document_name"] == document]
        if not match:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse(match[0])
