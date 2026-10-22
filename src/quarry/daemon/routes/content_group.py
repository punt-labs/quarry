"""The route-group base for endpoints that accept text content to index."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from quarry.content_guard import BinaryContent, UnindexableContentError
from quarry.daemon.routes.base import RouteGroup

if TYPE_CHECKING:
    from quarry.daemon.ingest_unit import IngestUnit


class ContentRouteGroup(RouteGroup):
    """Validate the fields every content-accepting route shares.

    ``remember``, ``learn``, and ``capture`` take free text and a ``memory_type``;
    their validators live here so no route can drift from the others (bug class 3).
    """

    @staticmethod
    def _reject_binary(text: str) -> JSONResponse | None:
        """Return a 400 carrying the classifier's reason if *text* is binary.

        A PDF or image decoded as text is refused before any job is built, so
        nothing is queued, embedded, or stored.  The sniff reads three bounded
        windows, so its cost does not grow with a 50 MiB body.  ``None`` means the
        text may proceed.
        """
        try:
            BinaryContent().sniff(text)
        except UnindexableContentError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return None

    def _require_content(self, body: dict[str, object], key: str) -> str | JSONResponse:
        """Return non-empty text ``body[key]`` that is not binary, or a 400."""
        text = self._require_text(body, key)
        if isinstance(text, JSONResponse):
            return text
        rejection = self._reject_binary(text)
        return text if rejection is None else rejection

    def _accept(self, kind: str, job: IngestUnit | JSONResponse) -> JSONResponse:
        """Queue a validated *job* as a *kind* task, or pass its 400 through."""
        if isinstance(job, JSONResponse):
            return job
        return self.submit(job, self.ctx.tasks.begin(kind))

    def _require_memory_type(self, body: dict[str, object]) -> str | JSONResponse:
        """Return ``body``'s ``memory_type`` (empty when absent), or a 400."""
        memory_type = self._str_field(body, "memory_type")
        rejection = self.reject_invalid_memory_type(memory_type)
        return memory_type if rejection is None else rejection
