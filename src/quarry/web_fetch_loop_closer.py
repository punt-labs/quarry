"""Nudge an agent off re-fetching a URL quarry already has under captures."""

from __future__ import annotations

import logging
from typing import Self, final
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


@final
class WebFetchLoopCloser:
    """Decide whether a fetched URL predates this fetch, and build the nudge.

    Owns both halves of the loop-closer feature: the daemon round-trip (fail-
    open on any client error) and the ``additionalContext`` wording an agent
    can act on — the URL in its log form, a suggested ``find`` query, and a note if
    the page is indexed under an older spelling; no stored name is printed.  Called
    from :func:`quarry.hooks.handle_post_web_fetch` BEFORE the capture send: a
    lookup run after would always match, since the send that follows would
    have just created it.
    """

    __slots__ = ("_cwd", "_url")

    _url: str
    _cwd: str

    def __new__(cls, url: str, cwd: str) -> Self:
        self = super().__new__(cls)
        self._url = url
        self._cwd = cwd
        return self

    def context(self) -> dict[str, object]:
        """Return the PostToolUse ``additionalContext`` envelope, or ``{}``.

        Fail-open: an unreachable daemon or any client error yields ``{}``
        silently — a failed lookup must never block or annotate the fetch.
        """
        from quarry.client import (  # noqa: PLC0415
            ClientConfigError,
            QuarryError,
            TargetResolver,
        )

        try:
            resp = TargetResolver.connect().captures_lookup(self._url, self._cwd)
        except (ClientConfigError, QuarryError):
            logger.debug("post-web-fetch: lookup unavailable, skipping nudge")
            return {}
        if not resp.matched:
            return {}
        return self._output(legacy=resp.legacy)

    def _output(self, *, legacy: bool) -> dict[str, object]:
        """Build the hookSpecificOutput envelope naming the URL and a find query.

        Shows the log form of the URL, never ``self._url`` raw and never a stored
        name — this text lands in the session transcript via ``additionalContext``,
        so a token in the query, or in a name an older build stored, must not
        survive into it.
        """
        from quarry.web_fetch_name import WebFetchName  # noqa: PLC0415

        query = self._suggested_query(WebFetchName.of(self._url))
        shown = WebFetchName.shown(self._url)
        lines = [
            (
                f"You already fetched {shown} earlier — use find "
                f"with a query like {query!r} instead of re-fetching next time."
            )
        ]
        if legacy:
            lines.append(
                "It is indexed under an older spelling of that URL; "
                "'quarry find' reaches it."
            )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "\n".join(lines),
            },
        }

    @staticmethod
    def _suggested_query(redacted_url: str) -> str:
        """Return the URL's last path segment, or its host when the path is bare."""
        parts = urlsplit(redacted_url)
        segment = parts.path.rstrip("/").rsplit("/", 1)[-1]
        return segment or parts.hostname or redacted_url
