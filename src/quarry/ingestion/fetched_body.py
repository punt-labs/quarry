"""A fetched HTTP body and the gate every fetch passes."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import ClassVar, Protocol, final
from urllib.error import HTTPError, URLError

from quarry.content_guard import UnindexableContentError
from quarry.ingestion.media_type_policy import MediaTypePolicy
from quarry.ingestion.ssrf_redirect import RedirectRejectedError
from quarry.url_safety import UrlRejectedError, UrlSafetyCheck


class FetchRefusedError(UnindexableContentError):
    """Raised when a fetch is refused or fails; the message never embeds the URL.

    Network libraries quote the raw URL (userinfo, query tokens) in their
    messages, so the fetch layer replaces every such failure with one of these:
    the reason is safe to log, to store as a task error, and to return to a
    client verbatim.
    """


class UrlChecker(Protocol):
    """The SSRF policy a gate delegates to."""

    def reject_reason(self, url: str) -> str | None:
        """Return ``None`` if *url* is safe to fetch, else why not."""
        ...


@final
@dataclass(frozen=True, slots=True)
class FetchGate:
    """The checks a fetch passes: before the socket, after redirects, before the body.

    Every rejection is a :class:`FetchRefusedError` (or, for a media type, its
    :class:`UnsupportedContentError` sibling) naming a rule, never the URL.
    """

    policy: MediaTypePolicy = field(default_factory=MediaTypePolicy)
    urls: UrlChecker = UrlSafetyCheck

    def check_url(self, url: str) -> None:
        """Raise unless *url* is HTTP(S) and resolves to a public address.

        Self-gates the initial URL (host + resolved address) BEFORE a socket is
        opened — defence in depth behind the route boundary, so a direct caller
        cannot reach an internal address.  Redirect hops are gated by the guarded
        opener; this covers the first hop.
        """
        if not url.lower().startswith(("http://", "https://")):
            msg = "URL rejected: only HTTP(S) URLs are supported"
            raise FetchRefusedError(msg)
        reason = self.urls.reject_reason(url)
        if reason is not None:
            msg = f"URL rejected: {reason}"
            raise FetchRefusedError(msg)

    def check_final_url(self, url: str) -> None:
        """Raise if the final URL (post-redirect) is unsafe/internal.

        The scheme is not re-checked: the opener only follows HTTP(S) hops, so
        the address policy is the only thing a redirect can newly violate.
        """
        reason = self.urls.reject_reason(url)
        if reason is not None:
            msg = f"final URL rejected: {reason}"
            raise FetchRefusedError(msg)

    @staticmethod
    def translate(exc: Exception) -> FetchRefusedError:
        """Return the URL-free refusal for a transfer failure.

        The SSRF policy's own rejections (a redirect hop, or the connect-time
        re-check after admission passed) name a rule, which an operator needs;
        every other exception quotes the raw URL, so only what kind of failure it
        was is reported.  An :class:`HTTPError` holds an open socket, so it is
        closed here, before the status is read out.
        """
        if isinstance(exc, HTTPError):
            code = exc.code
            with contextlib.suppress(OSError, ValueError):
                exc.close()
            return FetchRefusedError(f"fetch failed: HTTP {code}")
        if isinstance(exc, RedirectRejectedError):
            return FetchRefusedError(str(exc))
        if isinstance(exc, UrlRejectedError):
            return FetchRefusedError(f"URL rejected: {exc}")
        if isinstance(exc, TimeoutError):
            return FetchRefusedError("fetch failed: timed out")
        if isinstance(exc, URLError):
            return FetchRefusedError("fetch failed: cannot reach the host")
        return FetchRefusedError(f"fetch failed: {type(exc).__name__}")

    def check_media_type(self, media_type: str) -> None:
        """Raise :class:`UnsupportedContentError` unless the media type is text."""
        self.policy.require(media_type)


@final
@dataclass(frozen=True, slots=True)
class FetchedBody:
    """The decoded body of an HTTP fetch plus its declared media type.

    ``media_type`` is normalized on construction to the lower-cased
    ``Content-Type`` primary token (before any parameters) — ``"text/html"``,
    ``"application/json"``, ``"text/plain"``, or ``""`` when the response omitted
    the header.  Callers decide how to route the body: HTML through the
    extractor, everything else through the text pipeline.
    """

    text: str
    media_type: str

    _HTML: ClassVar[frozenset[str]] = frozenset({"text/html", "application/xhtml+xml"})

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "media_type", MediaTypePolicy.normalize(self.media_type)
        )

    @classmethod
    def from_header(cls, text: str, content_type: str) -> FetchedBody:
        """Return the body for a raw ``Content-Type`` header value."""
        return cls(text=text, media_type=content_type)

    @property
    def label(self) -> str:
        """Return the media type as safe to show in a message."""
        return MediaTypePolicy.label(self.media_type)

    @property
    def is_html(self) -> bool:
        """Return True when the body is HTML/XHTML or Content-Type was absent.

        A missing ``Content-Type`` is treated as HTML for backward
        compatibility with servers that omit the header — the contract of
        :meth:`WebFetcher.fetch`.
        """
        return not self.media_type or self.media_type in self._HTML
