"""The route-group base for endpoints that accept text content to index."""

from __future__ import annotations

from starlette.responses import JSONResponse

from quarry.content_guard import BinaryContent, UnindexableContentError
from quarry.daemon.routes.base import RouteGroup


class ContentRouteGroup(RouteGroup):
    """Validate the fields every content-accepting route shares.

    ``remember``, ``learn``, and ``capture`` take free text and a ``memory_type``;
    their validators live here so no route can drift from the others (bug class 3).
    """

    @staticmethod
    def _require_content(body: dict[str, object], key: str) -> str | JSONResponse:
        """Return non-empty text ``body[key]``, or a 400 naming the problem.

        Used for every free-text field a client sends to be indexed or fetched
        (content, lesson, source URL).  Besides presence, the text is sniffed for
        a binary payload: a PDF or image decoded as text is refused with the
        classifier's reason before any job is built, so nothing is queued,
        embedded, or stored.  The sniff reads three bounded windows, so its cost
        does not grow with a 50 MiB body.
        """
        text = RouteGroup._require_text(body, key)
        if isinstance(text, JSONResponse):
            return text
        try:
            BinaryContent().sniff(text)
        except UnindexableContentError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return text

    def _require_memory_type(self, body: dict[str, object]) -> str | JSONResponse:
        """Return ``body``'s ``memory_type`` (empty when absent), or a 400."""
        memory_type = self._str_field(body, "memory_type")
        rejection = self.reject_invalid_memory_type(memory_type)
        return memory_type if rejection is None else rejection
