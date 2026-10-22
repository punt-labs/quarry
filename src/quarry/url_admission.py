"""The single admission gate for a URL a caller wants fetched."""

from __future__ import annotations

from typing import final

from quarry.url_safety import UrlRejectedError, UrlSafetyCheck
from quarry.url_syntax import UrlSyntax


@final
class UrlAdmission:
    """Decide whether a URL may be fetched: plain syntax, HTTP(S), a public host.

    Every URL source passes here: the routes, a capture's source URL, each sitemap
    entry, each redirect hop and the final URL.  The answer is ``None`` for safe, or
    a reason that quotes nothing of the URL's userinfo, path or query: ``malformed
    URL`` when :class:`UrlSyntax` refuses it, ``unsupported scheme``, ``missing
    hostname``, or the address policy's reason, which names only the host.
    """

    __slots__ = ()

    @staticmethod
    def reject_reason(url: str) -> str | None:
        """Return ``None`` if *url* is safe to fetch, else why not; never raises."""
        try:
            parts = UrlSyntax.parse(url)
        except ValueError as exc:
            return str(exc)  # fixed text: the URL never reaches the reason
        host = parts.hostname
        if parts.scheme.lower() not in {"http", "https"}:
            return "unsupported scheme"
        if not host:
            return "missing hostname"
        try:
            UrlSafetyCheck.validated_addresses(host)
        except UrlRejectedError as exc:
            return str(exc)
        return None
