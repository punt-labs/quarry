"""Send scrubbed captures to the running daemon over the thin HTTP client.

Every hook that produces a capture — pre-compact, web-fetch, session-end,
web-search, read — routes through :class:`DaemonCaptureSender`.  Keeping the
four failure classes named in exactly one place prevents each caller from
re-inventing its own error branching.  Fire-and-forget: the daemon 202s a
capture before any embedding runs, so a healthy send is near instant and a
lost send is only a lost *capture*, never a lost transcript on disk.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.api import CaptureIngestRequest, IngestRequest
    from quarry.client import QuarryClient

logger = logging.getLogger(__name__)

# The daemon 202s a capture before any embedding runs, so a healthy send is
# near instant.  Cap it well below the client's 15s default: a saturated daemon
# must never make a compaction wait — the durable archive already holds the
# transcript.
_CAPTURE_SEND_TIMEOUT = 5.0


@final
class DaemonCaptureSender:
    """Post a capture request to the running daemon and translate failures.

    One instance per hook invocation; each ``send_*`` call opens a fresh
    connection via ``TargetResolver.connect()`` (the client is stateless).
    ``unreachable_log`` is the caller's own accountability string — a web fetch
    has no durable local copy so a lost send is genuinely lost, whereas a
    compaction can fall back to ``backfill-sessions`` — so the caller supplies
    the phrasing that reflects what recovery is actually available.
    """

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def send_capture(
        self, request: CaptureIngestRequest, *, unreachable_log: str
    ) -> bool:
        """Send an inline capture (transcript or fetched page) to the daemon."""
        return self._send(
            lambda client: client.capture(request, timeout=_CAPTURE_SEND_TIMEOUT),
            unreachable_log=unreachable_log,
        )

    def send_ingest_url(self, request: IngestRequest, *, unreachable_log: str) -> bool:
        """Ask the daemon to re-fetch and index a URL (the web-fetch fallback)."""
        return self._send(
            lambda client: client.ingest_url(request, timeout=_CAPTURE_SEND_TIMEOUT),
            unreachable_log=unreachable_log,
        )

    @staticmethod
    def _send(post: Callable[[QuarryClient], object], *, unreachable_log: str) -> bool:
        """Run *post* against a fresh client; ``False`` on any of four failure classes.

        The hook imports only the thin client — no engine.  A failed send is
        never fatal (fire-and-forget; the daemon 202s immediately), so this
        returns ``False`` rather than raising.  The four failure classes are
        logged distinctly so an operator is not misled: a local misconfiguration
        (e.g. a ``QUARRY_URL`` pointing at a refused cleartext remote) is a
        config error, not a down daemon; a genuine connection failure is
        "unreachable" (what that costs differs per caller, hence
        *unreachable_log*); a non-2xx response means the daemon is up but
        rejected the request (auth, server, validation), not "down"; a bare
        ``QuarryError`` is a reachable-but-broken daemon (malformed response).
        """
        from quarry.client import (  # noqa: PLC0415
            ClientConfigError,
            HttpError,
            QuarryConnectionError,
            QuarryError,
            TargetResolver,
        )

        try:
            post(TargetResolver.connect())
        except ClientConfigError as exc:
            logger.warning("daemon target misconfigured: %s", exc.message)
            return False
        except QuarryConnectionError:
            logger.warning("%s", unreachable_log)
            return False
        except HttpError as exc:
            logger.warning(
                "daemon rejected request: HTTP %s — %s", exc.status, exc.message
            )
            return False
        except QuarryError as exc:
            logger.warning("daemon send failed (malformed response): %s", exc.message)
            return False
        return True
