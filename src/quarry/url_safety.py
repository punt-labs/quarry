"""SSRF policy: validate a URL or host against the block policy.

``POST /ingest`` and every server-side fetch take a caller-supplied URL, so an
unguarded resolve-and-connect is an SSRF primitive.  This module owns the single
block policy — non-``http(s)`` schemes, a metadata/``.local`` hostname denylist,
and the private/loopback/link-local/reserved/multicast/CGNAT address classes —
and exposes it two ways that share one classifier:

* :meth:`UrlSafetyCheck.reject_reason` — the ``None``-means-safe admission gate
  callers use (route admission, final-URL re-checks).
* :meth:`UrlSafetyCheck.validated_addresses` — the fail-closed resolver the
  pinned connection calls *inside* ``connect`` so the socket targets an address
  drawn from the same resolution that was validated (no independent re-resolve).
"""

from __future__ import annotations

import ipaddress
import socket as socket_module
from urllib.parse import urlsplit

type IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UrlRejectedError(ValueError):
    """A URL, host, or resolved address failed the SSRF policy.

    A ``ValueError`` so the existing ``ValueError`` handling on both fetch paths
    (``WebFetcher.fetch``, ``GatedSitemapWebClient.get``) surfaces it as a URL
    rejection rather than a network error, and so ``RedirectRejectedError``
    remains its sibling.
    """


class UrlSafetyCheck:
    """The single SSRF block policy, shared by admission and connect-time pin."""

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
    def validated_addresses(cls, host: str) -> tuple[IpAddress, ...]:
        """Resolve *host* once and return every address, all validated safe.

        Fail-closed and all-records: the hostname denylist runs first, then a
        single ``getaddrinfo``, then every returned address is classified; the
        first blocked address (or a resolution/parse failure) raises
        :class:`UrlRejectedError`.  The result is therefore never empty — the
        method either raises or returns at least one safe address, which is what
        lets the pinned connection connect to a member of this set without a
        second, independently-resolved lookup.
        """
        cls._reject_hostname(host)
        addresses = cls._resolve_host(host)
        for addr in addresses:
            cls._reject_address(host, addr)
        return addresses

    @classmethod
    def reject_reason(cls, url: str) -> str | None:
        """Return ``None`` if *url* is safe to fetch, else a rejection reason.

        The ``None``-means-safe contract mirrors a boolean guard (PY-EH-4) and
        never raises for malformed input: a parse failure returns a reason, a
        bad scheme/host returns a reason, and every resolution/address rejection
        raised by :meth:`validated_addresses` is caught and rendered as one.
        """
        try:
            parsed = urlsplit(url)
            scheme = parsed.scheme.lower()
            host = parsed.hostname
        except ValueError as exc:
            # A malformed URL (bad IPv6 brackets, invalid port) must reject, not
            # crash: ``urlsplit`` and lazy ``.hostname``/``.port`` both raise.
            return f"malformed URL: {exc}"
        if scheme not in {"http", "https"}:
            return f"unsupported scheme {scheme!r}"
        if not host:
            return "missing hostname"
        try:
            cls.validated_addresses(host)
        except UrlRejectedError as exc:
            return str(exc)
        return None

    @classmethod
    def _reject_hostname(cls, host: str) -> None:
        """Reject by hostname alone, before any DNS resolution."""
        host_lower = host.lower()
        if host_lower in cls._METADATA_HOSTNAMES:
            raise UrlRejectedError(f"metadata hostname {host!r} is blocked")
        if host_lower.endswith(".local"):
            raise UrlRejectedError(f"'.local' hostname {host!r} is blocked")

    @classmethod
    def _resolve_host(cls, host: str) -> tuple[IpAddress, ...]:
        """Resolve *host* once, returning every address parsed to a value."""
        try:
            infos = socket_module.getaddrinfo(host, None)
        except (OSError, UnicodeError) as exc:
            # getaddrinfo raises UnicodeError (a ValueError subclass, NOT an
            # OSError) on an over-long IDNA label (>63 chars); catch both so the
            # resolution boundary always fails closed as UrlRejectedError rather
            # than letting a bare ValueError escape past the fetch boundary.
            raise UrlRejectedError(f"cannot resolve hostname {host!r}: {exc}") from exc
        return tuple(cls._parse_address(host, str(info[4][0])) for info in infos)

    @classmethod
    def _parse_address(cls, host: str, raw_addr: str) -> IpAddress:
        """Parse one resolved address, normalizing IPv4-mapped IPv6 to its IPv4.

        A mapped address (``::ffff:a.b.c.d``) is judged and pinned by its
        embedded IPv4; otherwise a mapped CGNAT/private address would slip the
        IPv4-only checks in :meth:`_reject_address`.
        """
        try:
            addr = ipaddress.ip_address(raw_addr)
        except ValueError as exc:
            msg = f"cannot parse resolved address for {host!r}"
            raise UrlRejectedError(msg) from exc
        mapped = getattr(addr, "ipv4_mapped", None)
        return mapped if mapped is not None else addr

    @classmethod
    def _reject_address(cls, host: str, addr: IpAddress) -> None:
        """Reject one already-parsed address if it falls in a blocked class."""
        if cls._is_blocked(addr):
            raise UrlRejectedError(f"host {host!r} resolves to blocked address {addr}")
        if addr.version == 4 and addr in cls._CGNAT_NETWORK:
            raise UrlRejectedError(f"host {host!r} resolves to CGNAT address {addr}")

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
