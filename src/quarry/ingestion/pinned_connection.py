"""HTTP(S) connections that connect only to a validated, pinned resolved address.

The SSRF admission gate resolves a host and rejects on a blocked address, but
that resolution is thrown away: ``http.client``'s ``connect`` re-resolves the
host independently, opening a DNS-rebinding TOCTOU window where an attacker's DNS
returns a public address at admission and an internal one at connect.

These connections close the window *by construction*.  There is exactly one
``getaddrinfo`` on the safety path — inside :meth:`connect` — and the socket
connects to an address drawn from that same validated result set, never
re-resolving.  The seam is ``http.client.HTTPConnection._create_connection``,
the instance attribute the stdlib itself sets to ``socket.create_connection``
and calls from ``connect``.  Rebinding it there (not overriding ``connect``'s
body) narrows only the TCP *target*: ``self.host`` is never mutated, so
``HTTPSConnection.connect`` still runs ``wrap_socket(server_hostname=self.host)``
against the hostname — SNI, certificate verification, and the ``Host`` header
stay bound to the hostname, never the pinned IP.  The pin narrows the address,
not the trust: the public-fetch TLS context (built by the opener) keeps the
system trust store, the deliberate opposite of the daemon-RPC pinned-CA context.
"""

from __future__ import annotations

import http.client
import socket
from collections.abc import Callable
from typing import final

from quarry.url_safety import UrlSafetyCheck


class PinnedHTTPConnection(http.client.HTTPConnection):
    """Plain-HTTP connection pinned to a connect-time-validated address."""

    # ``HTTPConnection.__init__`` binds this instance attribute to
    # ``socket.create_connection``; :meth:`connect` rebinds it to the pinned
    # substitute.  Declared so the re-bind is type-checked, not implicit.
    _create_connection: Callable[..., socket.socket]

    def connect(self) -> None:
        """Rebind the socket factory to the pinned one, then connect normally.

        The rebind is done here rather than in ``__init__`` so no ``__init__``
        override is needed (PY-CC-1): the stdlib binds ``_create_connection`` in
        its own ``__init__``, and ``connect`` runs after, so rebinding here wins
        and the stdlib ``connect`` body (socket create, ``TCP_NODELAY``, tunnel)
        is reused verbatim.
        """
        self._create_connection = self._pinned_create_connection
        super().connect()

    def _pinned_create_connection(
        self,
        address: tuple[str, int],
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        """Resolve+validate *address*'s host once, connect to a validated IP.

        Mirrors ``socket.create_connection``'s ``(address, timeout,
        source_address)`` signature so it is a drop-in for the stdlib seam.
        Every candidate is an already-validated IP literal, so the delegated
        ``socket.create_connection`` does a no-op parse (no second DNS) and owns
        per-attempt socket close on failure — no raw fd is held here (Class 1).
        A blocked resolution raises ``UrlRejectedError`` before any socket is opened.
        """
        host, port = address
        last_error: OSError | None = None
        for ip in UrlSafetyCheck.validated_addresses(host):
            try:
                return socket.create_connection(
                    (str(ip), port), timeout, source_address
                )
            except OSError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise OSError(f"no validated address for host {host!r}")


@final
class PinnedHTTPSConnection(http.client.HTTPSConnection, PinnedHTTPConnection):
    """HTTPS variant: the pinned TCP connect precedes the TLS handshake.

    Method-resolution order places :class:`PinnedHTTPConnection` between
    ``HTTPSConnection`` and ``HTTPConnection``, so ``HTTPSConnection.connect``'s
    ``super().connect()`` runs the pinned connect and *then*
    ``wrap_socket(server_hostname=self.host)`` still runs on the hostname.  TLS
    integrity is preserved with no reimplementation of the handshake.
    """
