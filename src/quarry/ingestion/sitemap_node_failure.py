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

    USP words each failure as a sentence that embeds the node's URL, so the reason
    is never logged or returned.  The one exact prefix trusted is the one that wraps
    a message our fetch layer wrote (a *transport* failure, whose remainder is the
    cause).  A sub-sitemap USP could not add is a document defect (a recursion, a
    corrupt gzip), so it is a *parse* failure, unless the web client recorded a bug
    while fetching: then it is *unexpected*.  Anything else is a *parse* failure.
    """

    url: str
    kind: FailureKind
    cause: str

    _FETCH: ClassVar[str] = "Unable to fetch sitemap from {url}: "
    _ADD: ClassVar[str] = "Unable to add sub-sitemap from URL {url}: "
    _STUTTER: ClassVar[str] = "fetch failed: "

    @classmethod
    def of(cls, node: InvalidSitemap, *, bug: str = "") -> Self:
        """Return the failure *node* records; *bug* is what the client recorded."""
        reason = node.reason
        fetch = cls._FETCH.format(url=node.url)
        if reason.startswith(fetch) and len(reason) > len(fetch):
            cause = reason[len(fetch) :].removeprefix(cls._STUTTER)
            return cls(node.url, "transport", cause)
        if bug and reason.startswith(cls._ADD.format(url=node.url)):
            return cls(node.url, "unexpected", "")
        return cls(node.url, "parse", "")

    @classmethod
    def in_tree(cls, sitemap: AbstractSitemap, *, bug: str = "") -> list[Self]:
        """Return the failure of every invalid node in *sitemap*, the root included."""
        return [
            cls.of(node, bug=bug)
            for node in (sitemap, *sitemap.all_sitemaps())
            if isinstance(node, InvalidSitemap)
        ]

    def log(self, logger: logging.Logger, *, speculative: bool = False) -> None:
        """Log one line: the redacted node URL and what kind of failure it was.

        A *speculative* walk (discovery) expects misses, so a transport or parse
        failure is DEBUG there; a bug is an ERROR either way.
        """
        if self.kind == "unexpected":
            level = logging.ERROR
        else:
            level = logging.DEBUG if speculative else logging.WARNING
        what = {
            "transport": self.cause,
            "unexpected": "unexpected error",
            "parse": "parse failure",
        }[self.kind]
        logger.log(level, "sitemap node failed [%s]: %s", HttpUrl.shown(self.url), what)
