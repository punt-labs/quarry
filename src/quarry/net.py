"""Host classification shared by the daemon bind gate and the client resolver.

Threat model: two failure directions must both be closed.

1. The daemon must refuse to bind a *remote-reachable* address on an
   auto-generated loopback token — that token is unreadable by the remote
   clients that would need it, so binding there without an operator-set key is
   false security.
2. The loopback bearer must be applied to *exactly* the addresses that never
   leave the host.  The ``127.0.0.1``-literal check this replaces misclassified
   ``localhost`` and ``::1`` as remote and wrongly demanded a key.

A single classifier both sides import keeps the daemon and the client from ever
disagreeing about what "loopback" means.
"""

from __future__ import annotations

import ipaddress
from typing import Self, final

# Hostnames that always resolve to this host only.  Anything else that is not a
# parseable loopback IP is treated as remote (fail closed).
_LOOPBACK_NAMES = frozenset({"localhost"})


@final
class LoopbackPolicy:
    """Classify one host as loopback and gate binds to non-loopback addresses."""

    _host: str

    def __new__(cls, host: str) -> Self:
        self = super().__new__(cls)
        self._host = host
        return self

    @property
    def is_loopback(self) -> bool:
        """Return whether the host reaches only this machine.

        Fails closed: an unparseable, non-``localhost`` name is NON-loopback, so
        it must carry an explicit key — an unknown name is never assumed safe.
        A bind-all address (``0.0.0.0``/``::``) is remote-reachable and so is
        never loopback.
        """
        # Normalize before matching.  Hostnames are case-insensitive
        # (RFC 4343); a single trailing dot is the DNS root label
        # (``localhost.`` == ``localhost``); surrounding whitespace is never
        # significant.  Without this a ``localhost.`` / `` localhost `` is
        # misread as remote and wrongly key-gated.  All three are harmless for
        # IP literals (``ip_address`` still parses the normalized form).
        host = self._host.strip().lower().removesuffix(".")
        if host in _LOOPBACK_NAMES:
            return True
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False
        # An IPv4-mapped IPv6 address (``::ffff:127.0.0.1``) is loopback iff its
        # embedded IPv4 is; ip_address's own ``is_loopback`` does not unwrap it.
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            return addr.ipv4_mapped.is_loopback
        return addr.is_loopback

    def enforce_bind_key(self, api_key: str | None) -> None:
        """Refuse a non-loopback bind that has no explicit key.

        Raises ``SystemExit`` rather than binding, because an auto-generated
        loopback token on a remote-reachable address is false security.
        """
        if not self.is_loopback and not api_key:
            msg = (
                f"Refusing to bind to {self._host} without an API key. "
                "Non-loopback hosts require authentication (set QUARRY_API_KEY)."
            )
            raise SystemExit(msg)
