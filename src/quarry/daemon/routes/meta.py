"""Server-meta routes: liveness, aggregate status, and the CA-cert bootstrap."""

from __future__ import annotations

from importlib.metadata import version
from typing import final

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from quarry.api import API_VERSION
from quarry.api.meta import FdHealth
from quarry.daemon.routes.base import RouteGroup
from quarry.fd_headroom import FdHeadroom
from quarry.ingestion.provider import ProviderSelection
from quarry.sync_registry import SyncRegistry

# The running package version, read once at import for the health snapshot.
_QUARRY_VERSION = version("punt-quarry")

# Naming symmetry with hooks._active_context: the daemon derives the captures
# sibling from the caller's collection so a client never has to spell both.
_CAPTURES_SUFFIX = "-captures"


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
