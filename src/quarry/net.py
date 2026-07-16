"""Host classification shared by the daemon bind gate and the client resolver.

Threat model: two failure directions must both be closed.

1. The daemon must refuse to bind a *remote-reachable* address on an
   auto-generated loopback token — that token is unreadable by the remote
   clients that would need it, so binding there without an operator-set key is
   false security.
2. The loopback *bearer* must reach ONLY the daemon that minted it.  A NAME
   (``localhost``) is resolver-controlled and on a dual-stack host can resolve
   to a co-tenant's ``::1``, so token presentation is gated on a LITERAL
   loopback IP (:attr:`is_literal_loopback`), never a name — while the bind
   gate (:attr:`is_loopback`) stays name-tolerant.  The two MUST NOT share one
   predicate: routing the bind gate's name-tolerance into token presentation is
   exactly how a name would leak the secret serve.token in transit.

One module both sides import keeps the daemon and the client from disagreeing
about what "loopback" means for each gate.
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
    def _normalized(self) -> str:
        """The host lowercased, whitespace-stripped, and de-trailing-dotted.

        Hostnames are case-insensitive (RFC 4343); a single trailing dot is the
        DNS root label (``localhost.`` == ``localhost``); surrounding whitespace
        is never significant.  Harmless for IP literals — ``ip_address`` still
        parses the normalized form.
        """
        return self._host.strip().lower().removesuffix(".")

    @property
    def is_loopback(self) -> bool:
        """Whether the host reaches only this machine (the BIND gate).

        Name-tolerant: ``localhost`` counts, so a loopback bind is never wrongly
        forced to carry an operator key.  This is the DAEMON/installer bind gate
        — deliberately distinct from token presentation, which needs the
        stricter literal-IP check (:attr:`is_literal_loopback`): a NAME must
        never trigger presentation of the secret serve.token.  Fails closed: an
        unparseable, non-``localhost`` name is NON-loopback.  A bind-all address
        (``0.0.0.0``/``::``) is remote-reachable and so never loopback.
        """
        if self._normalized in _LOOPBACK_NAMES:
            return True
        return self.is_literal_loopback

    @property
    def is_literal_loopback(self) -> bool:
        """Whether the host is a LITERAL loopback IP (the TOKEN-PRESENTATION gate).

        The live serve.token is a secret that must reach only the daemon that
        minted it.  A NAME like ``localhost`` is resolver-controlled — on a
        dual-stack host it can resolve to a co-tenant's ``::1`` — so presenting
        the token to a name would leak it in transit.  A literal IP cannot be
        redirected by resolution order.  Only ``127.0.0.0/8``, ``::1``, and
        IPv4-mapped IPv6 loopback qualify; a name (unparseable as an IP) is
        never a literal loopback.
        """
        try:
            addr = ipaddress.ip_address(self._normalized)
        except ValueError:
            return False
        # An IPv4-mapped IPv6 address (``::ffff:127.0.0.1``) is loopback iff its
        # embedded IPv4 is; ip_address's own ``is_loopback`` does not unwrap it.
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            return addr.ipv4_mapped.is_loopback
        return addr.is_loopback

    @property
    def canonical_host(self) -> str:
        """The IPv4 loopback literal for a loopback NAME, else the host unchanged.

        The managed daemon binds ``127.0.0.1``; storing the ambiguous name
        ``localhost`` lets a dual-stack resolver send the client (and its live
        serve.token) to a co-tenant's ``::1``.  Canonicalizing the name to the
        literal at write time pins the client to the address the daemon holds —
        a deliberate policy mapping, NOT an OS-resolver lookup.  A literal IP or
        a non-loopback host is returned unchanged.
        """
        if self._normalized in _LOOPBACK_NAMES:
            return "127.0.0.1"
        return self._host

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
