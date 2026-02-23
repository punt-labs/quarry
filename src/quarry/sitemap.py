"""Sitemap XML parser: fetch, parse, discover URLs, and filter entries."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_SITEMAP_TAG = f"{{{_NS}}}sitemap"
_SITEMAPINDEX_TAG = f"{{{_NS}}}sitemapindex"
_URLSET_TAG = f"{{{_NS}}}urlset"
_URL_TAG = f"{{{_NS}}}url"
_LOC_TAG = f"{{{_NS}}}loc"
_LASTMOD_TAG = f"{{{_NS}}}lastmod"


@dataclass(frozen=True)
class SitemapEntry:
    """A URL discovered from a sitemap."""

    loc: str
    lastmod: datetime | None


def fetch_sitemap(url: str, *, timeout: int = 30) -> str:
    """Fetch a sitemap URL and return the XML body as text.

    Raises:
        ValueError: If the URL is not HTTP(S) or the response is not XML.
        OSError: On network errors.
    """
    import urllib.request  # noqa: PLC0415
    from urllib.error import HTTPError, URLError  # noqa: PLC0415

    if not url.startswith(("http://", "https://")):
        msg = f"Only HTTP(S) URLs are supported: {url}"
        raise ValueError(msg)

    request = urllib.request.Request(  # noqa: S310
        url,
        headers={
            "User-Agent": "quarry/1.0 (+https://github.com/punt-labs/quarry)",
            "Accept": "application/xml, text/xml",
        },
    )
    _allowed_media_types = {
        "application/xml",
        "text/xml",
        "application/x-gzip",
        "text/html",  # some servers serve sitemaps as text/html
    }
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            final_url: str = resp.geturl()
            if not final_url.startswith(("http://", "https://")):
                msg = f"Redirect left HTTP(S): {final_url}"
                raise ValueError(msg)
            content_type: str = resp.headers.get("Content-Type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type and media_type not in _allowed_media_types:
                msg = f"URL returned non-XML content: {content_type}"
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


def _parse_lastmod(text: str) -> datetime | None:
    """Parse a sitemap <lastmod> value into a timezone-aware datetime.

    Supports ISO 8601 date (YYYY-MM-DD) and datetime formats.
    Returns None if parsing fails.
    """
    text = text.strip()
    if not text:
        return None

    # Try full datetime with timezone
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    # Handle trailing Z (common in sitemaps)
    if text.endswith("Z"):
        return _parse_lastmod(text[:-1] + "+00:00")

    logger.warning("Unparseable <lastmod>: %s", text)
    return None


def parse_sitemap(xml_text: str) -> tuple[list[SitemapEntry], list[str]]:
    """Parse sitemap XML into URL entries and child sitemap URLs.

    Returns:
        Tuple of (entries from <urlset>, child sitemap URLs from <sitemapindex>).
    """
    root = ET.fromstring(xml_text)  # noqa: S314
    tag = root.tag

    entries: list[SitemapEntry] = []
    children: list[str] = []

    if tag == _SITEMAPINDEX_TAG:
        for sitemap_el in root.findall(_SITEMAP_TAG):
            loc_el = sitemap_el.find(_LOC_TAG)
            if loc_el is not None and loc_el.text:
                children.append(loc_el.text.strip())
    elif tag == _URLSET_TAG:
        for url_el in root.findall(_URL_TAG):
            loc_el = url_el.find(_LOC_TAG)
            if loc_el is None or not loc_el.text:
                continue
            lastmod: datetime | None = None
            lastmod_el = url_el.find(_LASTMOD_TAG)
            if lastmod_el is not None and lastmod_el.text:
                lastmod = _parse_lastmod(lastmod_el.text)
            entries.append(SitemapEntry(loc=loc_el.text.strip(), lastmod=lastmod))
    else:
        # Try without namespace (some sitemaps omit it)
        if root.tag == "sitemapindex":
            for sitemap_el in root.findall("sitemap"):
                loc_el = sitemap_el.find("loc")
                if loc_el is not None and loc_el.text:
                    children.append(loc_el.text.strip())
        elif root.tag == "urlset":
            for url_el in root.findall("url"):
                loc_el = url_el.find("loc")
                if loc_el is None or not loc_el.text:
                    continue
                lastmod = None
                lastmod_el = url_el.find("lastmod")
                if lastmod_el is not None and lastmod_el.text:
                    lastmod = _parse_lastmod(lastmod_el.text)
                entries.append(
                    SitemapEntry(loc=loc_el.text.strip(), lastmod=lastmod)
                )
        else:
            msg = f"Unknown sitemap root element: {root.tag}"
            raise ValueError(msg)

    return entries, children


def discover_urls(
    url: str,
    *,
    timeout: int = 30,
    max_depth: int = 3,
) -> list[SitemapEntry]:
    """Fetch and parse a sitemap, recursing into sitemap indexes.

    Args:
        url: Sitemap URL.
        timeout: HTTP timeout in seconds.
        max_depth: Maximum recursion depth for sitemap indexes.

    Returns:
        Deduplicated flat list of all SitemapEntry found.
    """
    seen_urls: set[str] = set()
    all_entries: list[SitemapEntry] = []

    def _recurse(sitemap_url: str, depth: int) -> None:
        if depth > max_depth:
            logger.warning(
                "Sitemap recursion depth %d exceeded at %s", max_depth, sitemap_url
            )
            return
        if sitemap_url in seen_urls:
            return
        seen_urls.add(sitemap_url)

        logger.info("Fetching sitemap: %s (depth %d)", sitemap_url, depth)
        xml_text = fetch_sitemap(sitemap_url, timeout=timeout)
        entries, children = parse_sitemap(xml_text)

        for entry in entries:
            if entry.loc not in seen_urls:
                seen_urls.add(entry.loc)
                all_entries.append(entry)

        for child_url in children:
            _recurse(child_url, depth + 1)

    _recurse(url, 0)
    return all_entries


def filter_entries(
    entries: list[SitemapEntry],
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    limit: int = 0,
) -> list[SitemapEntry]:
    """Filter sitemap entries by URL path glob patterns.

    Args:
        entries: Sitemap entries to filter.
        include: If provided, only URLs whose path matches at least one pattern.
        exclude: URLs whose path matches any pattern are removed.
            Exclude takes precedence over include.
        limit: Maximum entries to return (0 = no limit).

    Returns:
        Filtered list of entries.
    """
    result: list[SitemapEntry] = []
    for entry in entries:
        path = urlparse(entry.loc).path

        if exclude and any(fnmatch(path, pat) for pat in exclude):
            continue
        if include and not any(fnmatch(path, pat) for pat in include):
            continue

        result.append(entry)

        if limit > 0 and len(result) >= limit:
            break

    return result
