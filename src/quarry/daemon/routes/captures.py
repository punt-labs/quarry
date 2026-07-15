"""The capture-push route: re-scrub and push enabled capture shadows."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.daemon.routes.base import RouteGroup
from quarry.http_guards import RequestGuards

if TYPE_CHECKING:
    from quarry.config import Settings

# The push request carries no body of note; cap it like the other option posts.
MAX_CAPTURES_BODY_BYTES = 16 * 1024


@final
class CaptureRoutes(RouteGroup):
    """Serve capture push — mirrors ``quarry captures push``."""

    async def push(self, request: Request) -> JSONResponse:
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp
        size_err = RequestGuards.check_body_size(request, MAX_CAPTURES_BODY_BYTES)
        if size_err is not None:
            return size_err
        results = await run_in_threadpool(self._push_registered, self.ctx.settings)
        return JSONResponse({"results": results})

    @staticmethod
    def _push_registered(settings: Settings) -> dict[str, dict[str, object]]:
        """Push every enabled project's redacted captures (the CLI path)."""
        from quarry.shadow import CaptureSync  # noqa: PLC0415

        results = CaptureSync.push_registered(settings, fail_open=True)
        return {col: res.to_dict() for col, res in results.items()}
