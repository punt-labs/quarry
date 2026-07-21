"""SSRF guard for URLs fetched server-side, gated at every hop of a fetch."""

from __future__ import annotations

import ipaddress
import socket as socket_module
from urllib.parse import urlsplit

type IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UrlSafetyCheck:
    """Reject ingest URLs that resolve to private or metadata addresses.

    ``POST /ingest`` fetches a caller-supplied URL server-side, so an
    unguarded fetch is an SSRF primitive.  This guard rejects URLs whose
    scheme is not ``http(s)`` and whose host resolves to a private, loopback,
    link-local, reserved, multicast, or CGNAT (RFC 6598) address, a ``.local``
    hostname, or a well-known cloud metadata endpoint.
    """

    # Cloud instance-metadata hosts, rejected regardless of DNS resolution to
    # harden against DNS-rebinding and TOCTOU attacks.
    _METADATA_HOSTNAMES = frozenset(
        {
            "169.254.169.254",
            "metadata.google.internal",
            "metadata",
            "instance-data.ec2.internal",
        }
    )

    # RFC 6598 Shared Address Space (Carrier-Grade NAT).  Python's
    # ``is_private`` predates RFC 6598 and does not cover this range.
    _CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

    @classmethod
    def reject_reason(cls, url: str) -> str | None:
        """Return ``None`` if *url* is safe to fetch, else a rejection reason.

        The ``None``-means-safe contract mirrors a boolean guard (PY-EH-4): the
        caller acts on presence-of-reason, not on a produced value.

        Note: this shares a known DNS-rebinding race with the downstream
        fetcher — the two resolutions are independent, so DNS an attacker
        controls could return a safe IP here and a private IP at the socket.
        Gating every hop against the resolved address is complementary to
        pinning the resolved IP through to connect (tracked as follow-up).
        """
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return f"unsupported scheme {parsed.scheme!r}"
        host = parsed.hostname
        if not host:
            return "missing hostname"
        return cls._reject_host(host) or cls._reject_resolved(host)

    @classmethod
    def _reject_host(cls, host: str) -> str | None:
        """Reject by hostname alone, before any DNS resolution."""
        host_lower = host.lower()
        if host_lower in cls._METADATA_HOSTNAMES:
            return f"metadata hostname {host!r} is blocked"
        if host_lower.endswith(".local"):
            return f"'.local' hostname {host!r} is blocked"
        return None

    @classmethod
    def _reject_resolved(cls, host: str) -> str | None:
        """Resolve *host* and reject on the first blocked address."""
        try:
            infos = socket_module.getaddrinfo(host, None)
        except OSError as exc:
            return f"cannot resolve hostname {host!r}: {exc}"
        for info in infos:
            reason = cls._reject_address(host, str(info[4][0]))
            if reason is not None:
                return reason
        return None

    @classmethod
    def _reject_address(cls, host: str, raw_addr: str) -> str | None:
        """Classify one resolved address and reject if it is blocked."""
        try:
            addr = ipaddress.ip_address(raw_addr)
        except ValueError:
            return f"cannot parse resolved address for {host!r}"
        if cls._is_blocked(addr):
            return f"host {host!r} resolves to blocked address {addr}"
        if addr.version == 4 and addr in cls._CGNAT_NETWORK:
            return f"host {host!r} resolves to CGNAT address {addr}"
        return None

    @staticmethod
    def _is_blocked(addr: IpAddress) -> bool:
        """Return whether *addr* falls in any category quarry refuses to fetch."""
        return (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        )
