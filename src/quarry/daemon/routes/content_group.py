"""The route-group base for endpoints that accept text content to index."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar, TypeIs, get_args

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from quarry.content_guard import BinaryContent, UnindexableContentError
from quarry.daemon.captured_body_ingest import IngestLabels
from quarry.daemon.routes.base import RouteGroup
from quarry.db.row_budget import RowBudget, RowBudgetError
from quarry.http_guards import RequestGuards
from quarry.http_url import HttpUrl
from quarry.ingestion.memory_type_guard import MemoryTypeGuard
from quarry.ingestion.web_ingest import FormatHint
from quarry.url_admission import UrlAdmission

if TYPE_CHECKING:
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.ingestion.ingest_context import MemoryType

logger = logging.getLogger(__name__)


class ContentRouteGroup(RouteGroup):
    """Validate what ``remember``, ``learn``, ``capture`` and ``ingest`` share.

    ``remember`` and ``capture`` take free text and run :meth:`_require_content`
    (the label cap, then the binary sniff).  ``learn`` runs the same two checks
    itself, through :meth:`_reject_oversized` and :meth:`_reject_binary`, because
    it also composes the lesson.  ``ingest`` takes a URL, so only the label cap
    applies.  ``remember``, ``capture`` and ``ingest`` validate ``memory_type``
    through :meth:`_require_memory_type` (``learn`` fixes its own), and all four
    queue their job through :meth:`_accept`, so no route can drift from the
    others.
    """

    _GUARD: ClassVar[BinaryContent] = BinaryContent()
    _BUDGET: ClassVar[RowBudget] = RowBudget()
    _FORMAT_HINTS: ClassVar[tuple[str, ...]] = get_args(FormatHint.__value__)
    _LABEL_FIELDS: ClassVar[tuple[str, ...]] = (
        "name",
        "document_name",
        "summary",
        "topic",
        "source",
        "agent_handle",
        "collection",
    )

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

    @classmethod
    def _reject_oversized(
        cls, body: dict[str, object], kind: str
    ) -> JSONResponse | None:
        """Return a 400 if a name, summary or routing field of *body* is over the cap.

        Each is copied onto every chunk row, so an oversized one could overflow a
        scan batch.  The reason names the field and the sizes, never the value, and
        the refusal is logged once with the route *kind*, like a binary one.
        """
        try:
            for key in cls._LABEL_FIELDS:
                # Checked as the job will read it: ``_str_field`` stringifies a
                # non-string value, so a list-valued label is held to the cap too.
                if value := body.get(key):
                    cls._BUDGET.require_label(key, str(value))
        except RowBudgetError as exc:
            logger.warning("%s refused: %s", kind, exc)
            return JSONResponse({"error": str(exc)}, status_code=400)
        return None

    def _require_content(
        self, body: dict[str, object], key: str, kind: str
    ) -> str | JSONResponse:
        """Return non-empty text ``body[key]`` that is not binary, or a 400.

        Every label field (``_LABEL_FIELDS``) is checked against the label cap
        first.
        """
        text = self._require_text(body, key)
        if isinstance(text, JSONResponse):
            return text
        rejection = self._reject_oversized(body, kind) or self._reject_binary(
            text, kind
        )
        return text if rejection is None else rejection

    async def _admit_url(self, raw: str) -> HttpUrl | JSONResponse:
        """Return *raw* as an admitted :class:`HttpUrl`, or a 400 saying why not.

        The one gate for every URL a request names (``ingest``'s source, a
        capture's ``source_url`` the daemon re-fetches): syntax, scheme and a
        public address, fail-closed, before any job is built.  The gate calls
        ``getaddrinfo``, which can block on DNS, so it runs in the threadpool.
        """
        reason = await run_in_threadpool(UrlAdmission.reject_reason, raw)
        if reason is not None:
            return JSONResponse({"error": f"URL rejected: {reason}"}, status_code=400)
        return HttpUrl(raw)

    def _accept(self, kind: str, job: IngestUnit | JSONResponse) -> JSONResponse:
        """Queue a validated *job* as a *kind* task, or pass its 400 through."""
        if isinstance(job, JSONResponse):
            return job
        return self.submit(job, self.ctx.tasks.begin(kind))

    @classmethod
    def _require_format_hint(cls, body: dict[str, object]) -> FormatHint | JSONResponse:
        """Return ``body``'s ``format_hint`` (default ``auto``), or a 400."""
        raw = cls._str_field(body, "format_hint", "auto")
        if not cls._is_format_hint(raw):
            choices = ", ".join(cls._FORMAT_HINTS)
            msg = f"unknown format_hint {raw!r}; expected one of {choices}"
            return JSONResponse({"error": msg}, status_code=400)
        return raw

    @classmethod
    def _is_format_hint(cls, raw: str) -> TypeIs[FormatHint]:
        return raw in cls._FORMAT_HINTS

    def _require_options(
        self, body: dict[str, object]
    ) -> tuple[bool, MemoryType, FormatHint] | JSONResponse:
        """Return ``(overwrite, memory_type, format_hint)`` of *body*, or a 400.

        ``overwrite`` defaults to true, as it does for ``remember`` and ``capture``.
        """
        overwrite = RequestGuards.coerce_bool_field(body, "overwrite", default=True)
        if isinstance(overwrite, JSONResponse):
            return overwrite
        memory_type = self._require_memory_type(body)
        if isinstance(memory_type, JSONResponse):
            return memory_type
        format_hint = self._require_format_hint(body)
        if isinstance(format_hint, JSONResponse):
            return format_hint
        return overwrite, memory_type, format_hint

    def _require_memory_type(
        self, body: dict[str, object]
    ) -> MemoryType | JSONResponse:
        """Return ``body``'s ``memory_type`` (empty when absent), or a 400."""
        memory_type = self._str_field(body, "memory_type")
        rejection = self.reject_invalid_memory_type(memory_type)
        if rejection is not None:
            return rejection
        return self._narrow(memory_type)

    @staticmethod
    def _narrow(raw: str) -> MemoryType:
        """Return *raw* as a storage ``MemoryType`` (the single narrowing point)."""
        return MemoryTypeGuard.require(raw)

    def _labels(
        self,
        memory_type: MemoryType,
        fields: dict[str, object],
        *,
        overwrite: bool,
        collection: str,
    ) -> IngestLabels:
        """Return the :class:`IngestLabels` of a request: the tags and its target."""
        return IngestLabels(
            overwrite,
            collection,
            self._str_field(fields, "agent_handle", ""),
            memory_type,
            self._str_field(fields, "summary", ""),
        )
