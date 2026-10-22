"""The PostToolUse hook that captures a page Claude Code just fetched."""

from __future__ import annotations

import logging
from typing import Self, final

from quarry._hook_trace import HookPayload, HookTrace
from quarry._stdlib import load_hook_config
from quarry.api import CaptureIngestRequest, IngestRequest
from quarry.daemon_capture import DaemonCaptureSender
from quarry.web_capture import WebFetchPayload
from quarry.web_fetch_loop_closer import WebFetchLoopCloser
from quarry.web_fetch_name import WebFetchName

logger = logging.getLogger(__name__)

# A web fetch writes NO durable local copy and backfill-sessions only re-ingests
# session transcripts, so a lost web capture is genuinely lost — the log must not
# promise a backfill that will never happen.
_WEB_FETCH_UNREACHABLE = (
    "web-fetch: daemon unreachable; page not indexed (re-fetch to retry)"
)


@final
class WebFetchHook:
    """Send the page Claude Code fetched to the daemon, and nudge on a repeat.

    Sends the already-fetched page to the daemon, which extracts, scrubs, and
    stores it in the project's ``<repo>-captures`` collection.  Uses the payload
    ``tool_response`` directly — no second fetch.  When the payload has no usable
    content, the daemon re-fetches through the SSRF-checked URL-ingest route
    instead.  The hook imports no engine — only the thin client and the
    lightweight URL/scrub helpers.  Looks up a PRIOR capture of this URL FIRST
    — after the (unconditional) send would always match — via
    :class:`~quarry.web_fetch_loop_closer.WebFetchLoopCloser`.

    A URL that could never be stored (a bad port, userinfo, whitespace) is
    skipped quietly: nothing about it is worth a log line or a daemon call.
    """

    __slots__ = ("_cwd", "_parsed", "_payload", "_trace")

    _payload: dict[str, object]
    _cwd: str
    _parsed: WebFetchPayload
    _trace: HookTrace

    def __new__(cls, payload: dict[str, object]) -> Self:
        self = super().__new__(cls)
        self._payload = payload
        self._cwd = HookPayload.as_dir(payload.get("cwd"))
        self._parsed = WebFetchPayload(payload)
        self._trace = HookTrace("post-web-fetch")
        return self

    def run(self) -> dict[str, object]:
        """Return the hook output: the loop-closer context, or ``{}`` to skip."""
        if self._cwd:
            on = load_hook_config(self._cwd).web_fetch
            self._trace.mark_config(on=on)
            if not on:
                self._trace.skip("config")
                return {}
        url = self._parsed.url
        if not url:
            self._trace.mark_payload(ok=False)
            self._trace.skip("no-url")
            return {}
        self._trace.mark_payload(ok=True)
        document_name = WebFetchName.maybe(url)
        if document_name is None:
            self._trace.skip("bad-url")
            return {}
        context = WebFetchLoopCloser(url, self._cwd).context()
        self._record(self._send(url, document_name))
        return context

    def _send(self, url: str, document_name: str) -> tuple[bool, str]:
        """Send the capture; return whether the daemon took it, and by which path."""
        sender = DaemonCaptureSender()
        content = self._parsed.content
        if content:
            # Primary: hand the raw HTML to the daemon (it extracts, scrubs,
            # chunks).  Carry the source URL so that if the HTML extracts to zero
            # chunks (a JS-rendered page) the daemon can re-fetch it server-side —
            # the capture route SSRF-gates source_url before the re-fetch — so the
            # page is captured, not silently dropped, and the client stays
            # engine-free.
            request = CaptureIngestRequest(
                content=content,
                cwd=self._cwd,
                document_name=document_name,
                format_hint="html",
                source_url=url,
            )
            sent = sender.send_capture(request, unreachable_log=_WEB_FETCH_UNREACHABLE)
            return sent, "inline"
        # Fallback: no usable content — the daemon re-fetches through the
        # SSRF-checked ingest route, scrubbing the page into <repo>-captures.
        logger.debug("post-web-fetch: no content in payload, re-fetching via daemon")
        fetch = IngestRequest(source=url, cwd=self._cwd, overwrite=True, scrub=True)
        sent = sender.send_ingest_url(fetch, unreachable_log=_WEB_FETCH_UNREACHABLE)
        return sent, "ingest-url"

    def _record(self, outcome: tuple[bool, str]) -> None:
        """Close the trace.  The sender already logged the specific failure class
        (misconfig, down, HTTP, malformed); the trace only needs the binary
        distinction so the entered line closes on every exit path.
        """
        sent, detail = outcome
        if sent:
            self._trace.capture(detail)
        else:
            self._trace.error("daemon-unreachable")
