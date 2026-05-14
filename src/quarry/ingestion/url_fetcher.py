"""URL fetching: retrieve HTML content from HTTP(S) URLs."""

from __future__ import annotations

from typing import Self


class UrlFetcher:
    """Fetch HTML content from HTTP(S) URLs.

    Stateless class with one public method. Exists as a class (not a
    bare function) so that ``UrlIngester`` can compose a
    ``_fetcher: UrlFetcher`` and tests can inject a mock.
    """

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def fetch(self, url: str, *, timeout: int = 30) -> str:
        """Fetch a URL and return the response body as text.

        Raises:
            ValueError: If the URL is not HTTP(S) or the response is not HTML.
            OSError: On network errors.
        """
        import urllib.request  # noqa: PLC0415
        from urllib.error import HTTPError, URLError  # noqa: PLC0415

        if not url.startswith(("http://", "https://")):
            msg = f"Only HTTP(S) URLs are supported: {url}"
            raise ValueError(msg)

        request = urllib.request.Request(  # noqa: S310
            url,
            headers={"User-Agent": "quarry/1.0 (+https://github.com/punt-labs/quarry)"},
        )
        allowed_media_types = {"text/html", "application/xhtml+xml"}
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
                final_url: str = resp.geturl()
                if not final_url.startswith(("http://", "https://")):
                    msg = f"Redirect left HTTP(S): {final_url}"
                    raise ValueError(msg)
                content_type: str = resp.headers.get("Content-Type", "")
                media_type = content_type.split(";", 1)[0].strip().lower()
                if media_type and media_type not in allowed_media_types:
                    msg = f"URL returned non-HTML content: {content_type}"
                    raise ValueError(msg)
                charset = resp.headers.get_content_charset() or "utf-8"
                body: bytes = resp.read()
                return body.decode(charset, errors="replace")
        except HTTPError as exc:
            msg = f"HTTP {exc.code} fetching {url}"
            raise ValueError(msg) from exc
        except URLError as exc:
            msg = f"Cannot reach {url}: {exc.reason}"
            raise OSError(msg) from exc
