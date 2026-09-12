"""Handlers for the four new agent-lifecycle hooks: SessionEnd,
PostToolUse:WebSearch, PostToolUse:Read, SubagentStop.

Each is a ``HookAgent`` staticmethod with the ``run_hook`` shape — a
``Callable[[dict[str, object]], dict[str, object]]`` — matching every existing
handler in :mod:`quarry.hooks`.  The orchestration logic (parse → filter →
send) lives in these methods; the substantive work (archive/scrub/send)
lives in the classes each handler delegates to.

**SubagentStop is a BLOCKING hook.**  ``HookAgent.subagent_stop`` must NEVER
populate a ``decision`` field or exit non-zero: a bug there hangs every
subagent in the session.  The design mandates ``{}`` on every path; the
handler tests assert this under crafted-adversarial payloads.
"""

from __future__ import annotations

import logging

from quarry._hook_trace import HookPayload, HookTrace, ReadAdmission
from quarry._stdlib import load_hook_config
from quarry.daemon_capture import DaemonCaptureSender
from quarry.ethos_handle import EthosConfig
from quarry.read_capture import ReadCaptureFilter, ReadPayload
from quarry.session_transcript import SessionTranscriptCapture, TranscriptCaptureOutcome
from quarry.web_search_capture import WebSearchPayload

logger = logging.getLogger(__name__)

_WEB_SEARCH_UNREACHABLE = (
    "web-search: daemon unreachable; digest not indexed (re-run to retry)"
)
_READ_UNREACHABLE = "post-read: daemon unreachable; file not indexed"


class HookAgent:
    """Namespace for the four agent-lifecycle hook handlers."""

    @staticmethod
    def session_end(payload: dict[str, object]) -> dict[str, object]:
        """Handle SessionEnd hook: capture the full session transcript unconditionally.

        Fires on every session close (unlike PreCompact, which only fires on
        context compaction) so a short session that never compacts still
        yields a durable capture.  Returns ``{}`` on every path — the session
        is already ending, there is no live user to surface a systemMessage
        to.
        """
        trace = HookTrace("post-session-end")
        cwd = HookPayload.as_dir(payload.get("cwd"))
        if not cwd:
            trace.skip("cwd")
            return {}
        on = load_hook_config(cwd).session_end
        trace.mark_config(on=on)
        if not on:
            trace.skip("config")
            return {}
        transcript_path = HookPayload.as_str(payload.get("transcript_path"))
        session_id = HookPayload.as_str(payload.get("session_id"))
        tp = (
            HookPayload.resolve_jsonl(transcript_path, label="session-end")
            if (transcript_path and session_id)
            else None
        )
        if tp is None:
            trace.mark_payload(ok=False)
            trace.skip("payload")
            return {}
        trace.mark_payload(ok=True)
        outcome = SessionTranscriptCapture(
            cwd=cwd,
            session_id=session_id,
            transcript_path=tp,
            label="session-end",
            agent_handle=EthosConfig.agent_handle_at(cwd),
        ).capture()
        HookAgent._trace_transcript_outcome(trace, outcome)
        return {}

    @staticmethod
    def _trace_transcript_outcome(
        trace: HookTrace, outcome: TranscriptCaptureOutcome
    ) -> None:
        """Emit the breadcrumb that matches the actual transcript-send outcome.

        A silent-skip is only visible when the trace tells the truth: an
        unreachable daemon must not read as ``capture``, and an empty
        transcript is a skip, not a send failure (parity with the
        ``handle_pre_compact`` branching in :mod:`quarry.hooks`).
        """
        if not outcome.text_captured:
            trace.skip("empty-transcript")
        elif outcome.sent:
            trace.capture()
        else:
            trace.error("daemon-unreachable")

    @staticmethod
    def post_web_search(payload: dict[str, object]) -> dict[str, object]:
        """Handle PostToolUse on WebSearch: file a scrubbed digest under captures.

        The daemon derives ``<repo>-captures`` from *cwd*; a session opened
        outside any registered directory files under ``default-captures``.
        The query text and result digest both flow through the daemon's
        ``ScrubbedIngestJob`` choke point, so no client-side scrub is needed
        here.
        """
        trace = HookTrace("post-web-search")
        cwd = HookPayload.as_dir(payload.get("cwd"))
        if cwd:
            on = load_hook_config(cwd).web_search
            trace.mark_config(on=on)
            if not on:
                trace.skip("config")
                return {}

        HookAgent._debug_search_shape(payload)
        parsed = WebSearchPayload(payload)
        digest = parsed.digest
        if digest is None:
            trace.mark_payload(ok=False)
            HookAgent._warn_no_search_digest(payload)
            trace.skip("no-digest")
            return {}
        trace.mark_payload(ok=True)

        from quarry.api import CaptureIngestRequest  # noqa: PLC0415

        sent = DaemonCaptureSender().send_capture(
            CaptureIngestRequest(
                content=digest,
                cwd=cwd,
                document_name=HookAgent._search_doc_name(parsed.query),
                format_hint="markdown",
            ),
            unreachable_log=_WEB_SEARCH_UNREACHABLE,
        )
        if sent:
            trace.capture()
        else:
            trace.error("daemon-unreachable")
        return {}

    @staticmethod
    def _debug_search_shape(payload: dict[str, object]) -> None:
        """Emit the DEBUG payload-shape probe used to diagnose G5 drift.

        Keys + tool_response type only — never contents, which may hold
        secrets.  The WARN in the caller fires on every silent skip, so
        the pair together makes the shape visible at production INFO.
        """
        logger.debug(
            "post-web-search: payload keys=%s tool_response_type=%s",
            sorted(payload.keys()),
            type(payload.get("tool_response")).__name__,
        )

    @staticmethod
    def _warn_no_search_digest(payload: dict[str, object]) -> None:
        """Emit the WARN line when WebSearchPayload yields no digest.

        Upgraded from DEBUG so a silent-skip is visible at production
        INFO — the operator's "proof they are happening" gap.  Logs
        shape metadata only (presence + length + tool_response type)
        because an operator's search box may hold tokens the same as
        any other free-text input; CWE-532 forbids persisting that to
        ``quarry.log`` (parity with :meth:`_debug_search_shape`).
        """
        parsed = WebSearchPayload(payload)
        logger.warning(
            "post-web-search: no result digest in payload "
            "(query_present=%s, query_len=%d, tool_response type=%s); "
            "skipping capture",
            parsed.query is not None,
            len(parsed.query or ""),
            type(payload.get("tool_response")).__name__,
        )

    @staticmethod
    def _search_doc_name(query: str | None) -> str:
        """Return the ``search: <query>`` document name, or a stable placeholder."""
        return f"search: {query or '(no query)'}"

    @staticmethod
    def post_read(payload: dict[str, object]) -> dict[str, object]:
        """Handle PostToolUse on Read: file the text under captures if it passes
        the filter.

        Config-gated (default OFF via ``HookConfig.read``) because ``Read``
        fires on every file the agent opens.  The four admission checks — in-
        tree exclusion, secret-path denylist, extension allowlist, size cap —
        run in order, fail-closed.  The daemon's ``ScrubbedIngestJob`` scrubs
        both the content and the document name (a raw filesystem path)
        server-side.
        """
        trace = HookTrace("post-read")
        cwd = HookPayload.as_dir(payload.get("cwd"))
        if not cwd:
            trace.skip("cwd")
            return {}
        on = load_hook_config(cwd).read
        trace.mark_config(on=on)
        if not on:
            trace.skip("config")
            return {}
        outcome = HookAgent._filter_and_send_read(payload)
        trace.mark_payload(ok=outcome != "payload")
        if outcome == "captured":
            trace.capture()
        elif outcome == "unreachable":
            trace.error("daemon-unreachable")
        else:
            trace.skip(outcome)
        return {}

    @staticmethod
    def _filter_and_send_read(payload: dict[str, object]) -> str:
        """Apply the four admission checks and send on pass; return the outcome.

        Return values classify the outcome for the trace: ``"captured"``,
        ``"unreachable"`` (send attempted but the daemon rejected or was
        down), ``"payload"`` (missing/empty inputs), ``"filter"`` (rejected
        by an admission check), ``"size"`` (byte cap exceeded), or
        ``"secret"`` (client-side secret pattern hit).
        """
        cwd = HookPayload.as_dir(payload.get("cwd"))
        parsed = ReadPayload(payload)
        file_path = parsed.file_path
        content = parsed.content
        if not file_path or not content:
            return "payload"

        # Fast checks (in-tree/denylist/extension) run before the content is
        # ever encoded — most rejections never need the byte length at all.
        filter_ = ReadCaptureFilter(resolver=ReadAdmission.collection_resolver_for(cwd))
        if not filter_.should_capture(file_path, cwd=cwd):
            return "filter"
        content_bytes = len(content.encode("utf-8", errors="replace"))
        if not filter_.should_capture(file_path, cwd=cwd, content_bytes=content_bytes):
            return "size"
        if ReadAdmission.content_has_secret(content):
            return "secret"

        from quarry.api import CaptureIngestRequest  # noqa: PLC0415

        sent = DaemonCaptureSender().send_capture(
            CaptureIngestRequest(
                content=content,
                cwd=cwd,
                document_name=file_path,
                format_hint="auto",
            ),
            unreachable_log=_READ_UNREACHABLE,
        )
        return "captured" if sent else "unreachable"

    @staticmethod
    def subagent_stop(payload: dict[str, object]) -> dict[str, object]:
        """Handle SubagentStop hook: capture the subagent's own transcript.

        **BLOCKING hook — never return a decision/block field.**
        ``SubagentStop`` fires blocking on Claude Code's side (a non-zero
        exit or a ``decision`` key forces the subagent to keep running),
        unlike every other hook this module registers.  Every path returns
        ``{}``.

        Per Ratification R4b (2026-08-30): the payload's
        ``agent_transcript_path`` is subagent-scoped and distinct from
        ``transcript_path`` (the parent's), so we archive the subagent path.
        Identity carrier is ``agent_id``, not ``session_id`` (which is the
        parent's).
        """
        trace = HookTrace("post-subagent-stop")
        cwd = HookPayload.as_dir(payload.get("cwd"))
        if not cwd:
            trace.skip("cwd")
            return {}
        on = load_hook_config(cwd).subagent_stop
        trace.mark_config(on=on)
        if not on:
            trace.skip("config")
            return {}

        transcript_path = HookPayload.as_str(payload.get("agent_transcript_path"))
        agent_id = HookPayload.as_str(payload.get("agent_id"))
        tp = (
            HookPayload.resolve_jsonl(transcript_path, label="subagent-stop")
            if (transcript_path and agent_id)
            else None
        )
        if tp is None:
            trace.mark_payload(ok=False)
            trace.skip("payload")
            return {}
        trace.mark_payload(ok=True)
        agent_type = HookPayload.as_str(payload.get("agent_type"))
        outcome = SessionTranscriptCapture(
            cwd=cwd,
            session_id=agent_id,
            transcript_path=tp,
            label="subagent-stop",
            agent_handle=agent_type or EthosConfig.agent_handle_at(cwd),
        ).capture()
        HookAgent._trace_transcript_outcome(trace, outcome)
        return {}
