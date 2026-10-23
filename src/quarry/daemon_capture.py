"""Send scrubbed captures to the running daemon over the thin HTTP client.

Every hook that produces a capture — pre-compact, web-fetch, session-end,
web-search, read, subagent-stop — routes through :class:`DaemonCaptureSender`.
Keeping the four failure classes named in exactly one place prevents each
caller from re-inventing its own error branching.  Fire-and-forget: the daemon
202s a capture before any embedding runs, so a healthy send is near instant and
a lost send is only a lost *capture*, never a lost transcript on disk.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, final

from quarry.send_outcome import SendOutcome

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.api import CaptureIngestRequest, IngestRequest, RememberRequest
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

    Three doors, one boundary: a scrubbed capture (``/v1/capture``), a
    server-side re-fetch (``/v1/ingest``), and a distilled memory
    (``/v1/remember``). All three carry the same short timeout and land in the
    daemon's scrub-before-store job, so no hook needs an engine.
    """

    __slots__ = ()

    def send_capture(
        self, request: CaptureIngestRequest, *, unreachable_log: str
    ) -> SendOutcome:
        """Send an inline capture (transcript or fetched page) to the daemon."""
        return self._send(lambda c: partial(c.capture, request), unreachable_log)

    def send_ingest_url(
        self, request: IngestRequest, *, unreachable_log: str
    ) -> SendOutcome:
        """Ask the daemon to re-fetch and index a URL (the web-fetch fallback)."""
        return self._send(lambda c: partial(c.ingest_url, request), unreachable_log)

    def send_remember(
        self, request: RememberRequest, *, unreachable_log: str
    ) -> SendOutcome:
        """File a distilled memory (a subagent's own report) via ``remember``."""
        return self._send(lambda c: partial(c.remember, request), unreachable_log)

    @staticmethod
    def _send(
        door: Callable[[QuarryClient], Callable[..., object]], unreachable_log: str
    ) -> SendOutcome:
        """Post through the bound client method *door* selects; never raise.

        The hook imports only the thin client — no engine.  A failed send is
        never fatal (fire-and-forget; the daemon 202s immediately), so this
        returns a falsy :class:`SendOutcome` rather than raising.  Failures are
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
            door(TargetResolver.connect())(timeout=_CAPTURE_SEND_TIMEOUT)
        except ClientConfigError as exc:
            outcome = SendOutcome.config(f"daemon target misconfigured: {exc.message}")
        except QuarryConnectionError:
            outcome = SendOutcome.unreachable(unreachable_log)
        except HttpError as exc:
            outcome = SendOutcome.rejected(exc.status, exc.message)
        except QuarryError as exc:
            outcome = SendOutcome.error(
                f"daemon send failed (malformed response): {exc.message}"
            )
        else:
            return SendOutcome(sent=True)
        logger.warning("%s", outcome.log)
        return outcome
