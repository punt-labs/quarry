"""Capture a Claude Code JSONL session transcript to the daemon.

Owns the transcript-specific pipeline every session-transcript hook shares:
archive raw JSONL under ``~/.punt-labs/quarry/sessions/``, extract session
artifacts, write a scrubbed ``.md`` capture next to the project, then POST a
``CaptureIngestRequest`` to the daemon.  Both PreCompact (mid-session) and
SessionEnd (guaranteed close) call this identically; SubagentStop calls it for
the subagent's own transcript, distinct from the parent's.

*label* names the producer (``"pre-compact"``, ``"session-end"``,
``"subagent-stop"``) so the scrub log line still identifies its origin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.daemon_capture import DaemonCaptureSender

if TYPE_CHECKING:
    from quarry.artifacts import SessionArtifacts
    from quarry.transcript_reader import TranscriptReader

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TranscriptCaptureOutcome:
    """Report what a transcript capture attempted.

    ``sent`` is whether the daemon accepted the wire payload; ``archived`` is
    whether the raw JSONL is now durable on disk.  Callers rely on the split:
    an unreachable daemon leaves ``archived=True, sent=False`` so
    ``backfill-sessions`` can recover the transcript later.
    """

    archived: bool
    sent: bool
    text_captured: bool


@final
class SessionTranscriptCapture:
    """Archive, scrub, and post a Claude Code session transcript.

    One instance per hook invocation.  ``session_id`` is the identity the
    capture files under — the parent session id for PreCompact and SessionEnd,
    the subagent id for SubagentStop (that carrier decision belongs to the
    caller, not this class).  ``agent_handle`` is empty when no ethos identity
    covers *cwd*; the daemon still routes to ``<repo>-captures`` either way.
    """

    __slots__ = ("_agent_handle", "_cwd", "_label", "_session_id", "_transcript_path")

    _cwd: str
    _session_id: str
    _transcript_path: Path
    _label: str
    _agent_handle: str

    def __new__(
        cls,
        *,
        cwd: str,
        session_id: str,
        transcript_path: Path,
        label: str,
        agent_handle: str = "",
    ) -> Self:
        self = super().__new__(cls)
        self._cwd = cwd
        self._session_id = session_id
        self._transcript_path = transcript_path
        self._label = label
        self._agent_handle = agent_handle
        return self

    def capture(self) -> TranscriptCaptureOutcome:
        """Run archive → scrub → daemon-post; return what actually happened.

        Failures at each stage funnel to the outcome flags; nothing raises out
        of this method.  The archive attempt is idempotent — calling capture
        twice for the same session_id supersedes the earlier archive rather
        than accumulating duplicates.
        """
        from quarry.artifacts import (  # noqa: PLC0415
            extract_artifacts,
            format_artifacts_header,
        )
        from quarry.transcript_reader import TranscriptReader  # noqa: PLC0415

        reader = TranscriptReader(self._transcript_path)
        archived = self._archive(reader)

        raw_text = reader.text()
        if not raw_text:
            logger.debug("%s: no conversation text found", self._label)
            return TranscriptCaptureOutcome(
                archived=archived, sent=False, text_captured=False
            )

        artifacts = extract_artifacts(raw_text)
        header = format_artifacts_header(artifacts)
        wire_text = f"{header}\n\n{raw_text}" if header else raw_text

        if self._cwd:
            iso_timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._write_local_capture(
                project_dir=Path(self._cwd),
                timestamp=iso_timestamp,
                artifacts=artifacts,
                text=raw_text,
            )

        sent = self._send(wire_text)
        return TranscriptCaptureOutcome(
            archived=archived, sent=sent, text_captured=True
        )

    def _archive(self, reader: TranscriptReader) -> bool:
        """Copy the raw JSONL under sessions/ and dedup prior archives for it."""
        sessions_dir = Path.home() / ".punt-labs" / "quarry" / "sessions"
        try:
            reader.archive(self._session_id, sessions_dir)
        except OSError:
            logger.exception("%s: archival failed, proceeding with ingest", self._label)
            return False
        return True

    def _write_local_capture(
        self,
        *,
        project_dir: Path,
        timestamp: str,
        artifacts: SessionArtifacts,
        text: str,
    ) -> None:
        """Write the scrubbed ``.md`` capture file next to the project."""
        from quarry.capture import CaptureRequest, CaptureWriter  # noqa: PLC0415

        CaptureWriter().write(
            CaptureRequest(
                project_dir=project_dir,
                session_id=self._session_id,
                timestamp=timestamp,
                artifacts=artifacts,
                text=text,
                label=self._label,
            )
        )

    def _send(self, wire_text: str) -> bool:
        """Post the wire text to the daemon; return whether the send succeeded."""
        from quarry.api import CaptureIngestRequest  # noqa: PLC0415

        request = CaptureIngestRequest(
            content=wire_text,
            cwd=self._cwd,
            session_id=self._session_id,
            agent_handle=self._agent_handle,
            format_hint="markdown",
        )
        unreachable = (
            f"{self._label}: daemon unreachable; transcript archived, "
            "run backfill-sessions to index it"
        )
        return DaemonCaptureSender().send_capture(request, unreachable_log=unreachable)
