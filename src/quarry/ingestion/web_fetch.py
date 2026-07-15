"""Fetch a URL over HTTP(S) and return validated HTML text."""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError

if TYPE_CHECKING:
    from http.client import HTTPResponse

_ALLOWED_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_USER_AGENT = "quarry/1.0 (+https://github.com/punt-labs/quarry)"


@dataclass(frozen=True, slots=True)
class WebFetcher:
    """Fetches HTML over HTTP(S), rejecting non-HTTP schemes and non-HTML bodies.

    A small value object that keeps the fetch policy — allowed schemes, allowed
    media types, redirect validation, and the User-Agent — in one place rather
    than inlined in the ingestion pipeline.
    """

    timeout: int = 30

    def fetch(self, url: str) -> str:
        """Fetch *url* and return the decoded response body.

        Raises:
            ValueError: If the URL is not HTTP(S) or the response is not HTML.
            OSError: On network errors.
        """
        if not url.startswith(("http://", "https://")):
            msg = f"Only HTTP(S) URLs are supported: {url}"
            raise ValueError(msg)

        request = urllib.request.Request(  # noqa: S310
            url,
            headers={"User-Agent": _USER_AGENT},
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=self.timeout
            ) as resp:
                return self._decode_html(resp)
        except HTTPError as exc:
            msg = f"HTTP {exc.code} fetching {url}"
            raise ValueError(msg) from exc
        except URLError as exc:
            msg = f"Cannot reach {url}: {exc.reason}"
            raise OSError(msg) from exc

    @staticmethod
    def _decode_html(resp: HTTPResponse) -> str:
        """Validate the final URL and media type, then decode the body to text."""
        final_url: str = resp.url
        if not final_url.startswith(("http://", "https://")):
            msg = f"Redirect left HTTP(S): {final_url}"
            raise ValueError(msg)
        content_type: str = resp.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type and media_type not in _ALLOWED_MEDIA_TYPES:
            msg = f"URL returned non-HTML content: {content_type}"
            raise ValueError(msg)
        charset = resp.headers.get_content_charset() or "utf-8"
        body: bytes = resp.read()
        return body.decode(charset, errors="replace")
