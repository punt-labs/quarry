"""A urllib opener that re-gates every redirect hop and pins every connection.

urllib's default ``HTTPRedirectHandler`` follows 30x responses automatically
with no per-hop check, so a caller-supplied public URL that 302s to a private,
loopback, link-local, CGNAT, or cloud-metadata address would reach an internal
service.  :data:`GUARDED_OPENER` replaces that handler with one that gates each
redirect target against its resolved address -- the same :class:`UrlSafetyCheck`
the ingest route runs on the initial source -- and refuses an unsafe hop before
it is opened.  It also replaces urllib's default HTTP(S) handlers with the pinned
ones (:mod:`quarry.ingestion.pinned_opener`), so each hop is connected to a
connect-time-validated IP, closing the DNS-rebinding TOCTOU window.  The whole
chain is covered because every hop is the target of the hop before it.
"""

from __future__ import annotations

import contextlib
import ssl
import urllib.request
from typing import IO, TYPE_CHECKING, final

from quarry.ingestion.pinned_opener import PinnedHTTPHandler, PinnedHTTPSHandler
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
        """Return the one gated, pinned, http(s)-only opener for server fetches.

        Assembled by hand rather than via ``urllib.request.build_opener`` so the
        handler set is a closed allowlist, not a default set minus overrides:
        the per-hop SSRF redirect gate, the pinned HTTP(S) handlers (each hop
        connects to a connect-time-validated IP -- see
        :mod:`quarry.ingestion.pinned_connection`), an empty ``ProxyHandler({})``
        so HTTP_PROXY/HTTPS_PROXY are NOT honored (the fetch goes DIRECT, never
        to an unvalidated proxy that would reintroduce SSRF), and the two error
        handlers http needs.  ``build_opener`` would additionally install
        ``FTPHandler``/``FileHandler``/``DataHandler``; omitting them makes the
        opener STRUCTURALLY unable to open ``ftp://``, ``file://``, or ``data:``
        -- ``UnknownHandler`` turns any non-http(s) scheme into a ``URLError`` --
        so a caller that forgets its own scheme pre-check still cannot reach a
        non-http(s) surface.  The pinned HTTPS handler gets an explicit
        ``create_default_context`` (system trust store, ``check_hostname`` on):
        the pin narrows the address, not the trust (the opposite of the
        daemon-RPC pinned-CA context).
        """
        opener = urllib.request.OpenerDirector()
        for handler in (
            cls(),  # per-hop SSRF redirect gate (301/302/303/307/308)
            PinnedHTTPHandler(),
            PinnedHTTPSHandler(context=ssl.create_default_context()),
            urllib.request.ProxyHandler({}),  # empty: ignore env proxies
            urllib.request.HTTPErrorProcessor(),  # route status codes to handlers
            urllib.request.HTTPDefaultErrorHandler(),  # unhandled code -> HTTPError
            urllib.request.UnknownHandler(),  # any non-http(s) scheme -> URLError
        ):
            opener.add_handler(handler)
        return opener

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
        This pre-check is defense in depth ahead of the connection-level pin,
        and it closes the intermediate 3xx response.
        """
        reason = UrlSafetyCheck.reject_reason(newurl)
        if reason is not None:
            # urllib's http_error_30x calls this BEFORE it reads/closes fp, so
            # raising here would leak the intermediate 3xx response's fd on every
            # blocked hop.  Close it first (Class-1 fd hygiene); a close failure
            # must not mask the SSRF rejection, so it is suppressed.
            with contextlib.suppress(OSError, ValueError):
                fp.close()
            raise RedirectRejectedError(f"redirect target rejected: {reason}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# One shared opener for all server-side fetches: every redirect hop is gated and
# every connection is pinned to a connect-time-validated address.
GUARDED_OPENER: urllib.request.OpenerDirector = (
    SsrfGuardedRedirectHandler.build_opener()
)
