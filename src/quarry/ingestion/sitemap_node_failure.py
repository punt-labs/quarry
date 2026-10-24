"""Say why one sitemap node failed, without ever quoting the URL or the exception."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal, Self, final

from usp.objects.sitemap import InvalidSitemap

from quarry.http_url import HttpUrl

if TYPE_CHECKING:
    from usp.objects.sitemap import AbstractSitemap

type FailureKind = Literal["transport", "unexpected", "parse"]


@final
@dataclass(frozen=True, slots=True)
class NodeFailure:
    """One invalid node of a sitemap tree, sorted into what went wrong.

    USP words each failure as a sentence that embeds the node's URL, and an
    unexpected error appends the exception's own text, so the reason is never
    logged or returned.  Only two exact prefixes are trusted: the one that wraps a
    message our fetch layer wrote (a *transport* failure, whose remainder is the
    cause), and the one that wraps an exception from a bug (*unexpected*).  Anything
    else is a *parse* failure.
    """

    url: str
    kind: FailureKind
    cause: str

    _FETCH: ClassVar[str] = "Unable to fetch sitemap from {url}: "
    _ADD: ClassVar[str] = "Unable to add sub-sitemap from URL {url}: "
    _STUTTER: ClassVar[str] = "fetch failed: "

    @classmethod
    def of(cls, node: InvalidSitemap) -> Self:
        """Return the failure *node* records."""
        reason = node.reason
        fetch = cls._FETCH.format(url=node.url)
        if reason.startswith(fetch) and len(reason) > len(fetch):
            cause = reason[len(fetch) :].removeprefix(cls._STUTTER)
            return cls(node.url, "transport", cause)
        if reason.startswith(cls._ADD.format(url=node.url)):
            return cls(node.url, "unexpected", "")
        return cls(node.url, "parse", "")

    @classmethod
    def in_tree(cls, sitemap: AbstractSitemap) -> list[Self]:
        """Return the failure of every invalid node in *sitemap*, the root included."""
        return [
            cls.of(node)
            for node in (sitemap, *sitemap.all_sitemaps())
            if isinstance(node, InvalidSitemap)
        ]

    def log(self, logger: logging.Logger) -> None:
        """Log one line: the redacted node URL and what kind of failure it was."""
        level = logging.ERROR if self.kind == "unexpected" else logging.WARNING
        what = {
            "transport": self.cause,
            "unexpected": "unexpected error",
            "parse": "parse failure",
        }[self.kind]
        logger.log(level, "sitemap node failed [%s]: %s", HttpUrl.shown(self.url), what)
