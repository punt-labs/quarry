"""Server-meta routes: liveness, aggregate status, and the CA-cert bootstrap."""

from __future__ import annotations

from importlib.metadata import version
from typing import final

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from quarry.api import API_VERSION
from quarry.daemon.routes.base import RouteGroup
from quarry.db.storage import dir_size_bytes
from quarry.ingestion.provider import ProviderSelection
from quarry.sync_registry import SyncRegistry

# The running package version, read once at import for the health snapshot.
_QUARRY_VERSION = version("punt-quarry")


@final
class MetaRoutes(RouteGroup):
    """Liveness, aggregate status, and CA-cert bootstrap."""

    def health(self, _request: Request) -> JSONResponse:
        """Return liveness plus warm ``state`` and version negotiation fields."""
        return JSONResponse(
            {
                "status": "ok",
                "uptime_seconds": round(self.ctx.uptime_seconds, 1),
                "state": self.ctx.state,
                "api_version": API_VERSION,
                "quarry_version": _QUARRY_VERSION,
            }
        )

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

        db_size_bytes = (
            dir_size_bytes(settings.lancedb_path)
            if settings.lancedb_path.exists()
            else 0
        )

        return JSONResponse(
            {
                "document_count": doc_count,
                "collection_count": len(cols),
                "chunk_count": chunks,
                "registered_directories": len(regs),
                "database_path": str(settings.lancedb_path),
                "database_size_bytes": db_size_bytes,
                "embedding_model": settings.embedding_model,
                "provider": ProviderSelection.display_cached(),
                "embedding_dimension": settings.embedding_dimension,
            }
        )
