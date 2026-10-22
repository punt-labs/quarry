"""The route-group base for endpoints that accept text content to index."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from starlette.responses import JSONResponse

from quarry.content_guard import BinaryContent, UnindexableContentError
from quarry.daemon.captured_body_ingest import IngestLabels
from quarry.daemon.routes.base import RouteGroup

if TYPE_CHECKING:
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.ingestion.ingest_context import MemoryType

logger = logging.getLogger(__name__)


class ContentRouteGroup(RouteGroup):
    """Validate what ``remember``, ``learn``, ``capture`` and ``ingest`` share.

    The three content routes take free text, sniffed for a binary payload by
    :meth:`_require_content`; all four routes validate ``memory_type`` through
    :meth:`_require_memory_type` and queue their job through :meth:`_accept`, so
    no route can drift from the others.
    """

    _GUARD: ClassVar[BinaryContent] = BinaryContent()

    @classmethod
    def _reject_binary(cls, text: str, kind: str) -> JSONResponse | None:
        """Return a 400 carrying the classifier's reason if *text* is binary.

        A PDF or image decoded as text is refused before any job is built, so
        nothing is queued, embedded, or stored.  The sniff reads three bounded
        windows, so its cost does not grow with the body size.  The refusal is
        logged once with the route *kind* and the reason, both URL-free.  ``None``
        means the text may proceed.
        """
        try:
            cls._GUARD.sniff(text)
        except UnindexableContentError as exc:
            logger.warning("%s refused: %s", kind, exc)
            return JSONResponse({"error": str(exc)}, status_code=400)
        return None

    def _require_content(
        self, body: dict[str, object], key: str, kind: str
    ) -> str | JSONResponse:
        """Return non-empty text ``body[key]`` that is not binary, or a 400."""
        text = self._require_text(body, key)
        if isinstance(text, JSONResponse):
            return text
        rejection = self._reject_binary(text, kind)
        return text if rejection is None else rejection

    def _accept(self, kind: str, job: IngestUnit | JSONResponse) -> JSONResponse:
        """Queue a validated *job* as a *kind* task, or pass its 400 through."""
        if isinstance(job, JSONResponse):
            return job
        return self.submit(job, self.ctx.tasks.begin(kind))

    def _require_memory_type(
        self, body: dict[str, object]
    ) -> MemoryType | JSONResponse:
        """Return ``body``'s ``memory_type`` (empty when absent), or a 400."""
        memory_type = self._str_field(body, "memory_type")
        rejection = self.reject_invalid_memory_type(memory_type)
        if rejection is not None:
            return rejection
        return IngestLabels.memory_type_of(memory_type)
