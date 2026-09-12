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
    can act on — the URL, a suggested ``find`` query, and the stored document
    name when the daemon supplies one.  Called from
    :func:`quarry.hooks.handle_post_web_fetch` BEFORE the capture send: a
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
        return self._output(resp.document_name)

    def _output(self, document_name: str | None) -> dict[str, object]:
        """Build the hookSpecificOutput envelope naming the URL and a find query.

        Uses the redacted (query/fragment/userinfo-stripped) form of the URL,
        never ``self._url`` raw — this text lands in the session transcript via
        ``additionalContext``, so any secret token in the query string must not
        survive into it.
        """
        from quarry.capture_url import CaptureUrl  # noqa: PLC0415

        redacted_url = CaptureUrl.for_web_fetch(self._url)
        lines = [
            f"You already fetched {redacted_url} earlier — use find with a "
            f"query like {self._suggested_query(redacted_url)!r} instead of "
            "re-fetching next time."
        ]
        if document_name:
            lines.append(
                f"Indexed as {document_name!r} — "
                f"'quarry show {document_name}' shows it directly."
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
