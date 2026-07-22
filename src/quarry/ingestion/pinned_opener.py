"""urllib handlers that route HTTP(S) opens through the pinned connections.

``urllib.request``'s stock ``HTTPHandler``/``HTTPSHandler`` hardcode the stdlib
connection classes in ``http_open``/``https_open``.  These subclasses override
only the connection class passed to ``do_open`` — the sole hook for injecting a
custom connection — so every fetch built on them resolves-validates-and-pins its
target inside ``connect`` (see :mod:`quarry.ingestion.pinned_connection`).  Being
subclasses of the stock handlers, ``urllib.request.build_opener`` drops its
defaults in favour of these, so no unpinned connection can slip into the chain.
"""

from __future__ import annotations

import http.client
import ssl
import urllib.request
from typing import final

from quarry.ingestion.pinned_connection import (
    PinnedHTTPConnection,
    PinnedHTTPSConnection,
)


@final
class PinnedHTTPHandler(urllib.request.HTTPHandler):
    """Open plain-HTTP requests through :class:`PinnedHTTPConnection`."""

    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        """Open *req* using the pinned plain-HTTP connection class."""
        return self.do_open(PinnedHTTPConnection, req)


@final
class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """Open HTTPS requests through :class:`PinnedHTTPSConnection`.

    ``do_open`` is given ``self._context`` verbatim — the handler does not
    invent a context, so whatever trust store the opener configured (system
    roots, for public fetch) is what verifies the certificate.
    """

    # ``HTTPSHandler.__init__`` stores the context here; declared so the
    # ``do_open`` hand-off is type-checked (typeshed leaves it undeclared).
    _context: ssl.SSLContext

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        """Open *req* using the pinned HTTPS connection class and stored context."""
        return self.do_open(PinnedHTTPSConnection, req, context=self._context)
