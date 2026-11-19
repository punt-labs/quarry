"""Server-meta routes: liveness, aggregate status, insights, CA-cert bootstrap."""

from __future__ import annotations

from typing import final

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from quarry.api.insights import InsightsResponse
from quarry.daemon.routes.base import RouteGroup
from quarry.daemon.routes.health_snapshot import HealthSnapshot
from quarry.daemon.routes.insights_builder import InsightsBuilder
from quarry.daemon.routes.status_snapshot import StatusSnapshot
from quarry.home_paths import QuarryHome
from quarry.query_log import get_query_log

# Naming symmetry with hooks._active_context: the daemon derives the captures
# sibling from the caller's collection so a client never has to spell both.
_CAPTURES_SUFFIX = "-captures"


@final
class MetaRoutes(RouteGroup):
    """Liveness, aggregate status, and CA-cert bootstrap."""

    def health(self, _request: Request) -> JSONResponse:
        """Return liveness, warm ``state``, version fields, and daemon fd headroom."""
        return JSONResponse(
            HealthSnapshot(
                uptime_seconds=self.ctx.uptime_seconds, state=self.ctx.state
            ).payload()
        )

    @staticmethod
    def ca_cert(request: Request) -> Response:  # noqa: ARG004
        """Serve the CA certificate PEM for TOFU bootstrap.

        Auth-exempt so the client can fetch it before login.
        Returns 404 with JSON error if no cert file exists.
        """
        ca_path = QuarryHome.daemon_ca()
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
        """Authenticate and report the daemon's live storage and engine status."""
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp

        return JSONResponse(StatusSnapshot(self.ctx).payload())

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
        """Return the recall-telemetry snapshot behind ``quarry insights``.

        A disabled toggle short-circuits before ``get_query_log`` -- opening
        (and so creating) the telemetry database file for a store the operator
        turned off would surprise a "disabled means untouched" reading of the
        setting.
        """
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp

        settings = self.ctx.settings
        if not settings.telemetry_enabled:
            return JSONResponse(InsightsResponse.disabled().model_dump())

        query_log = get_query_log(settings.telemetry_path)
        response = InsightsBuilder(self.ctx.database, query_log).build()
        return JSONResponse(response.model_dump())
