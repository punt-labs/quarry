"""The search route: hybrid search over the daemon's single database."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, final

from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.daemon.routes.base import RouteGroup
from quarry.query_log import get_query_log
from quarry.query_log_types import QueryEvent, QueryHit
from quarry.results import SearchFilter
from quarry.retrieval import SearchService
from quarry.retrieval.config import RetrievalConfig
from quarry.scrub import scrub

if TYPE_CHECKING:
    from starlette.datastructures import QueryParams

    from quarry.results import SearchResult

logger = logging.getLogger(__name__)

# Surface recorded when a caller omits ``?surface=`` -- a bare HTTP client that
# never threads provenance is not "cli" or "mcp", so this earns its own honest
# label rather than silently defaulting to one of the named surfaces.
_DEFAULT_SURFACE = "unknown"

# The closed vocabulary ``SearchRequest.surface`` documents but does not
# enforce (PY-TS-14 -- kept an open ``str`` there for forward-compat with a new
# caller). This route is where an unrecognized value earns the fallback
# instead: provenance is best-effort telemetry, so a stray or future value
# must never turn into a 400 and break the search itself.
_KNOWN_SURFACES = frozenset({"cli", "mcp", "http", "plugin"})


@dataclass(frozen=True, slots=True)
class _SearchOutcome:
    """One completed search's inputs and results, bundled for telemetry.

    Pushes what would otherwise be six parameters on the telemetry-recording
    methods into a single value object (PY-OO-3): the search route already
    assembles every field below to answer the request, so telemetry recording
    borrows them rather than re-deriving or re-threading them individually.
    """

    query: str
    surface: str
    search_filter: SearchFilter
    limit: int
    latency_ms: float
    results: list[SearchResult]


@final
class SearchRoutes(RouteGroup):
    """Serve search — clamp the limit, build the filter, rank hits, log telemetry."""

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
        config = RetrievalConfig(
            decay_rate=self.ctx.settings.retrieval_decay_rate,
            lesson_boost=self.ctx.settings.retrieval_lesson_boost,
        )
        started = time.perf_counter()
        results = SearchService(self.ctx.query_database, config).search(
            query, query_vector, search_filter, limit
        )
        latency_ms = (time.perf_counter() - started) * 1000
        formatted = [r.to_dict() for r in results]

        if self.ctx.settings.telemetry_enabled:
            outcome = _SearchOutcome(
                query=query,
                surface=self._coerce_surface(params.get("surface", "")),
                search_filter=search_filter,
                limit=limit,
                latency_ms=latency_ms,
                results=results,
            )
            self._record_telemetry(outcome)

        # DEBUG per the level policy in quarry.logging_config: search is the
        # daemon's highest-frequency request, so a count per query would bury
        # the operational record it shares a file with.
        logger.debug("Search results=%d", len(formatted))
        return JSONResponse(
            {"query": query, "total_results": len(formatted), "results": formatted}
        )

    def _record_telemetry(self, outcome: _SearchOutcome) -> None:
        """Record one ``query_events`` row + N ``query_hits`` rows, scrubbed first.

        A telemetry write is boundary I/O the search response must survive
        (PY-EH boundary I/O, not internal defensive coding): a locked
        database or a full disk here must never turn an otherwise-successful
        search into a 500, so any failure is logged and swallowed.
        """
        try:
            query_log = get_query_log(self.ctx.settings.telemetry_path)
            query_log.record(self._build_event(outcome), self._build_hits(outcome))
        except Exception:  # boundary I/O, see docstring -- must not fail `find`
            logger.exception("recall telemetry write failed; search results unaffected")

    @staticmethod
    def _build_event(outcome: _SearchOutcome) -> QueryEvent:
        """Return the scrubbed ``query_events`` row for *outcome*."""
        scrubbed, _counts = scrub(outcome.query)
        search_filter = outcome.search_filter
        return QueryEvent(
            ts=datetime.now(UTC).isoformat(),
            surface=outcome.surface,
            agent_handle=search_filter.agent_handle or "",
            collection=search_filter.collection or "",
            filters_json=json.dumps(SearchRoutes._filter_dict(search_filter)),
            limit_n=outcome.limit,
            latency_ms=round(outcome.latency_ms, 3),
            result_count=len(outcome.results),
            query_scrubbed=scrubbed,
            query_len=len(outcome.query),
        )

    @staticmethod
    def _build_hits(outcome: _SearchOutcome) -> list[QueryHit]:
        """Return one ``query_hits`` row per ranked result in *outcome*."""
        return [
            QueryHit(
                rank=rank,
                document_name=r.document_name,
                collection=r.collection,
                chunk_index=r.chunk_index,
                score=r.similarity,
                hit_agent_handle=r.agent_handle,
                memory_type=r.memory_type,
            )
            for rank, r in enumerate(outcome.results, start=1)
        ]

    @staticmethod
    def _filter_dict(search_filter: SearchFilter) -> dict[str, str]:
        """Return *search_filter*'s non-empty fields as a dict for ``filters_json``."""
        fields = (
            ("collection", search_filter.collection),
            ("document", search_filter.document),
            ("page_type", search_filter.page_type),
            ("source_format", search_filter.source_format),
            ("agent_handle", search_filter.agent_handle),
            ("memory_type", search_filter.memory_type),
        )
        return {name: value for name, value in fields if value}

    @staticmethod
    def _coerce_surface(raw: str) -> str:
        """Return *raw* if it names a known surface, else :data:`_DEFAULT_SURFACE`.

        Covers both an absent ``?surface=`` (empty string) and a value outside
        the closed set -- a future caller identifying itself with a name this
        set hasn't learned yet must still get a recorded search, not a 400.
        """
        return raw if raw in _KNOWN_SURFACES else _DEFAULT_SURFACE

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
