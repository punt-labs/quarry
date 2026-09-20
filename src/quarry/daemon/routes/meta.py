"""Server-meta routes: liveness, aggregate status, insights, CA-cert bootstrap."""

from __future__ import annotations

import sys
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import version
from typing import TYPE_CHECKING, final

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from quarry.api import API_VERSION
from quarry.api.meta import FdHealth
from quarry.daemon.routes.base import RouteGroup
from quarry.fd_headroom import FdHeadroom
from quarry.ingestion.provider import ProviderSelection
from quarry.query_log import get_query_log
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from quarry.query_log import QueryLog, RecallAggregate

# The running package version, read once at import for the health snapshot.
_QUARRY_VERSION = version("punt-quarry")

# Naming symmetry with hooks._active_context: the daemon derives the captures
# sibling from the caller's collection so a client never has to spell both.
_CAPTURES_SUFFIX = "-captures"

# Recency bands for the insights hit-decay breakdown, checked in order --
# each band's upper bound in days since the hit's chunk was ingested.
_DECAY_BANDS: tuple[tuple[str, int], ...] = (
    ("0-7d", 7),
    ("7-30d", 30),
    ("30-90d", 90),
    ("90d+", sys.maxsize),
)
_UNKNOWN_BAND = "unknown"


@final
class MetaRoutes(RouteGroup):
    """Liveness, aggregate status, and CA-cert bootstrap."""

    def health(self, _request: Request) -> JSONResponse:
        """Return liveness, warm ``state``, version fields, and daemon fd headroom."""
        fd = self._fd_headroom()
        return JSONResponse(
            {
                "status": "ok",
                "uptime_seconds": round(self.ctx.uptime_seconds, 1),
                "state": self.ctx.state,
                "api_version": API_VERSION,
                "quarry_version": _QUARRY_VERSION,
                "fd": fd.model_dump() if fd is not None else None,
            }
        )

    @staticmethod
    def _fd_headroom() -> FdHealth | None:
        """Sample the daemon's own open-fd headroom as an :class:`FdHealth`.

        Runs in the daemon process, so the sample *is* the resident daemon's real
        descriptor state — the number doctor must report, not the short-lived
        CLI's shell ulimit. A sample that raises (``EMFILE`` mid-scan or a
        platform with no fd directory) yields ``None`` so the health endpoint
        never 500s on the very exhaustion it exists to surface; doctor renders it
        as a degraded advisory. Returning the model makes :class:`FdHealth` the
        single owner of the fd wire shape (bug-class-3).
        """
        try:
            headroom = FdHeadroom.sample()
        except OSError:
            return None
        return FdHealth(open_fds=headroom.open_fds, soft_limit=headroom.soft_limit)

    @staticmethod
    def ca_cert(request: Request) -> Response:  # noqa: ARG004
        """Serve the CA certificate PEM for TOFU bootstrap.

        Auth-exempt so the client can fetch it before login.
        Returns 404 with JSON error if no cert file exists.
        """
        from quarry.tls import TLS_DIR  # noqa: PLC0415

        ca_path = TLS_DIR / "ca.crt"
        if not ca_path.exists():
            return JSONResponse(
                {"error": "No CA certificate found. Run 'quarry install' first."},
                status_code=404,
            )
        return PlainTextResponse(
            ca_path.read_text(),
            media_type="application/x-pem-file",
        )

    def status(self, request: Request) -> JSONResponse:
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp

        settings = self.ctx.settings
        chunks = self.ctx.database.store.count()
        cols = self.ctx.database.catalog.list_collections()
        doc_count = sum(c["document_count"] for c in cols)

        if settings.registry_path.exists():
            conn = SyncRegistry(settings.registry_path)
            try:
                regs = conn.list_registrations()
            finally:
                conn.close()
        else:
            regs = []

        return JSONResponse(
            {
                "document_count": doc_count,
                "collection_count": len(cols),
                "chunk_count": chunks,
                "registered_directories": len(regs),
                "database_path": str(settings.lancedb_path),
                "embedding_model": settings.embedding_model,
                "provider": ProviderSelection.display_cached(),
                "embedding_dimension": settings.embedding_dimension,
            }
        )

    def coverage(self, request: Request) -> JSONResponse:
        """Return per-repo counts for ``collection`` and its captures sibling.

        Reads ``?collection=<repo>`` from the query string; a missing or blank
        value earns a 400. The captures sibling is derived server-side as
        ``<collection>-captures`` so the wire contract carries one identifier,
        not two — the CLI/hook never has to spell the naming rule.
        """
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp
        collection = request.query_params.get("collection", "").strip()
        if not collection:
            return JSONResponse(
                {"error": "Missing required parameter: collection"},
                status_code=400,
            )
        counts = self.ctx.database.catalog.coverage(
            collection, f"{collection}{_CAPTURES_SUFFIX}"
        )
        return JSONResponse(dict(counts))

    def insights(self, request: Request) -> JSONResponse:
        """Return the recall-telemetry snapshot behind ``quarry insights``."""
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp

        settings = self.ctx.settings
        query_log = get_query_log(settings.telemetry_path)
        agg = query_log.insights.aggregate()
        return JSONResponse(
            {
                "telemetry_enabled": settings.telemetry_enabled,
                "total_queries": agg.total_queries,
                "empty_result_rate": agg.empty_result_rate,
                "p50_latency_ms": agg.p50_latency_ms,
                "p95_latency_ms": agg.p95_latency_ms,
                "top_empty_queries": self._empty_query_rows(agg),
                "per_collection_hits": self._collection_hit_rows(agg),
                "per_agent_recall": self._agent_recall_rows(agg),
                "memory_queries": agg.memory_queries,
                "knowledge_queries": agg.knowledge_queries,
                "hit_decay_bands": self._decay_band_rows(query_log),
            }
        )

    @staticmethod
    def _empty_query_rows(agg: RecallAggregate) -> list[dict[str, object]]:
        return [
            {"query_scrubbed": r["label"], "count": r["count"]}
            for r in agg.top_empty_queries
        ]

    @staticmethod
    def _collection_hit_rows(agg: RecallAggregate) -> list[dict[str, object]]:
        return [
            {"collection": r["label"], "hit_count": r["count"]}
            for r in agg.per_collection_hits
        ]

    @staticmethod
    def _agent_recall_rows(agg: RecallAggregate) -> list[dict[str, object]]:
        return [
            {"agent_handle": r["label"], "query_count": r["count"]}
            for r in agg.per_agent_recall
        ]

    def _decay_band_rows(self, query_log: QueryLog) -> list[dict[str, object]]:
        return [
            {"band": band, "hit_count": count}
            for band, count in self._hit_decay_bands(query_log).items()
        ]

    def _hit_decay_bands(self, query_log: QueryLog) -> dict[str, int]:
        """Bucket recent hits by the age of the document chunk they came from.

        Joins :meth:`QueryLog.insights.recent_hits` (document identity only,
        engine-free) against the live catalog's ``ingestion_timestamp`` --
        the join lives here, not in ``query_log.py``, because only the route
        holds ``ctx.database``.
        """
        refs = query_log.insights.recent_hits()
        if not refs:
            return {}
        ingested_at = self._document_ingestion_index()
        now = datetime.now(UTC)
        counts: Counter[str] = Counter()
        for ref in refs:
            ts = ingested_at.get((ref["collection"], ref["document_name"]))
            counts[self._band(ts, now) if ts is not None else _UNKNOWN_BAND] += 1
        return dict(counts)

    def _document_ingestion_index(self) -> dict[tuple[str, str], datetime]:
        """Map ``(collection, document_name)`` to its ingestion timestamp."""
        index: dict[tuple[str, str], datetime] = {}
        for doc in self.ctx.database.catalog.list_documents():
            try:
                ts = datetime.fromisoformat(doc["ingestion_timestamp"])
            except ValueError:
                continue
            index[(doc["collection"], doc["document_name"])] = ts
        return index

    @staticmethod
    def _band(ingested_at: datetime, now: datetime) -> str:
        """Return the recency-band label *ingested_at* falls into as of *now*."""
        age_days = (now - ingested_at).days
        for label, max_days in _DECAY_BANDS:
            if age_days <= max_days:
                return label
        return _DECAY_BANDS[-1][0]
