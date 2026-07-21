"""A urllib opener that re-runs the SSRF gate on every redirect hop.

urllib's default ``HTTPRedirectHandler`` follows 30x responses automatically
with no per-hop check, so a caller-supplied public URL that 302s to a private,
loopback, link-local, CGNAT, or cloud-metadata address would reach an internal
service.  :data:`GUARDED_OPENER` replaces that handler with one that gates each
redirect target against its resolved address -- the same :class:`UrlSafetyCheck`
the ingest route runs on the initial source -- and refuses an unsafe hop before
it is opened.  The whole chain is covered because every hop is the target of the
hop before it.
"""

from __future__ import annotations

import urllib.request
from typing import IO, TYPE_CHECKING, final

from quarry.url_safety import UrlSafetyCheck

if TYPE_CHECKING:
    from http.client import HTTPMessage


class RedirectRejectedError(ValueError):
    """A redirect target failed the SSRF gate; the hop was refused, not followed.

    A ``ValueError`` so :meth:`WebFetcher.fetch` surfaces it as an invalid-URL
    rejection alongside its other reject reasons, rather than as a network error.
    """


@final
class SsrfGuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject a redirect target that fails the SSRF gate before following it."""

    @classmethod
    def build_opener(cls) -> urllib.request.OpenerDirector:
        """Return an opener whose redirect handler is this SSRF gate.

        ``build_opener`` swaps urllib's default redirect handler for this
        subclass (it replaces a default handler when given a subclass of it) and
        keeps every other default handler.
        """
        return urllib.request.build_opener(cls())

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Refuse an unsafe ``newurl``; otherwise defer to the base handler.

        Raising here (rather than returning ``None``) aborts the fetch: the
        ``OpenerDirector`` never opens the target, so no connection to the
        internal address is attempted.  The gate resolves ``newurl``'s host, so
        a public hostname that resolves to an internal address is caught too.
        """
        reason = UrlSafetyCheck.reject_reason(newurl)
        if reason is not None:
            raise RedirectRejectedError(f"redirect target rejected: {reason}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# One shared opener for all server-side fetches: every redirect hop is gated.
GUARDED_OPENER: urllib.request.OpenerDirector = (
    SsrfGuardedRedirectHandler.build_opener()
)
