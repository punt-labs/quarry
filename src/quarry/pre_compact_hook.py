"""The PreCompact hook that captures the conversation before it is compacted."""

from __future__ import annotations

import logging
from typing import Self, final

from quarry._hook_trace import HookPayload, HookTrace
from quarry._stdlib import load_hook_config
from quarry.backfill_reach import BackfillReach
from quarry.send_outcome import SendOutcome
from quarry.session_transcript import SessionTranscriptCapture, TranscriptSource

logger = logging.getLogger(__name__)


@final
class PreCompactHook:
    """Archive the raw transcript, write the scrubbed capture, send it to the daemon.

    Returns the systemMessage immediately so compaction is never blocked, and a
    down daemon still leaves the durable local copies; the message then says so,
    and names ``backfill-sessions`` only for a session that command will visit.
    """

    __slots__ = ("_payload", "_trace")

    _payload: dict[str, object]
    _trace: HookTrace

    def __new__(cls, payload: dict[str, object]) -> Self:
        self = super().__new__(cls)
        self._payload = payload
        self._trace = HookTrace("pre-compact")
        return self

    def run(self) -> dict[str, object]:
        """Return the hook output: the capture notice or warning, or ``{}``."""
        target = self._target()
        if target is None:
            self._trace.skip("payload")
            return {}
        self._trace.mark_payload(ok=True)

        outcome = SessionTranscriptCapture.for_session(target).capture()

        if not outcome.text_captured:
            self._trace.skip("empty-transcript")
            return {}

        if not outcome.sent:
            self._trace.error(outcome.failure)
            return {
                "systemMessage": SendOutcome.advice(
                    outcome.failure,
                    archived=outcome.archived,
                    written=outcome.written,
                    backfillable=BackfillReach.includes(target.cwd),
                )
            }

        self._trace.capture()
        return {
            "systemMessage": (
                "Capturing this session's conversation (background). "
                "Search with /find or show to retrieve it."
            ),
        }

    def _target(self) -> TranscriptSource | None:
        """Return the transcript source for a capturable compaction.

        ``None`` means the hook must no-op — the documented skip contract, not a
        failure: disabled by config, a missing ``transcript_path``/``session_id``,
        or a non-JSONL transcript suffix (defense-in-depth).  ``cwd`` may be empty
        (an unregistered directory still archives and ingests); the other two are
        required.
        """
        cwd = HookPayload.as_dir(self._payload.get("cwd"))
        if cwd and not load_hook_config(cwd).compaction:
            logger.debug("pre-compact: disabled by config")
            return None
        transcript_path = HookPayload.as_str(self._payload.get("transcript_path"))
        session_id = HookPayload.as_str(self._payload.get("session_id"))
        if not transcript_path or not session_id:
            logger.debug("pre-compact: missing transcript_path or session_id")
            return None
        resolved = HookPayload.resolve_jsonl(transcript_path, label="pre-compact")
        if resolved is None:
            return None
        return TranscriptSource(cwd, session_id, resolved, "pre-compact")
