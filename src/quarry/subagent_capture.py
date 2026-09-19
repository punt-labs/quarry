"""SubagentStop's capture: the raw transcript plus the subagent's own report."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, final

from quarry.daemon_capture import DaemonCaptureSender
from quarry.ethos_handle import EthosConfig
from quarry.session_transcript import (
    SessionTranscriptCapture,
    TranscriptCaptureOutcome,
    TranscriptSource,
)
from quarry.subagent_report import SubagentReport
from quarry.transcript_reader import TranscriptReader


@final
@dataclass(frozen=True, slots=True)
class SubagentCaptureOutcome:
    """What a SubagentStop capture did: the raw row, the identity, the memory."""

    raw: TranscriptCaptureOutcome
    handle: str
    distilled: bool


@final
class SubagentCapture:
    """Capture a subagent's transcript and file its final report as a memory.

    Two rows from one event: the raw transcript lands in ``<repo>-captures``
    exactly as before (now with a summary), and — only when the subagent is a
    registered identity with something to report — its final assistant turn
    lands in ``memory-<handle>`` as an ``observation`` through the daemon's
    scrub-before-store ``remember`` route. No engine runs in the hook; the
    second POST carries the same 5 s cap as the first, and both rows come
    from one reader, so the transcript is parsed once.
    """

    __slots__ = ("_agent_type", "_parent_session_id", "_source")

    _source: TranscriptSource
    _agent_type: str
    _parent_session_id: str

    def __new__(
        cls, source: TranscriptSource, *, agent_type: str, parent_session_id: str
    ) -> Self:
        self = super().__new__(cls)
        self._source = source
        self._agent_type = agent_type
        self._parent_session_id = parent_session_id
        return self

    def capture(self) -> SubagentCaptureOutcome:
        """Run the raw capture, then file the report; never raise.

        Without a handle a ``remember`` would route to ``default`` — a memory
        nobody owns — so an unattributed subagent gets the raw capture only. A
        report is filed only after the raw capture found text, so the two rows
        for one agent never disagree about whether the session had content.
        """
        handle = EthosConfig.subagent_handle_at(self._agent_type, self._source.cwd)
        reader = TranscriptReader(self._source.transcript_path)
        report = SubagentReport.from_transcript(
            reader,
            handle=handle,
            agent_id=self._source.session_id,
            parent_session_id=self._parent_session_id,
            agent_type=self._agent_type,
        )
        raw = SessionTranscriptCapture(
            self._source, reader, agent_handle=handle, summary=report.summary
        ).capture()
        distilled = False
        if handle and not report.is_empty and raw.text_captured:
            distilled = DaemonCaptureSender().send_remember(
                report.remember_request(),
                unreachable_log=(
                    f"{self._source.label}: daemon unreachable; report for "
                    f"{handle} not filed (raw transcript archived)"
                ),
            )
        return SubagentCaptureOutcome(raw=raw, handle=handle, distilled=distilled)
