"""Capture a Claude Code JSONL session transcript to the daemon.

Owns the transcript-specific pipeline every session-transcript hook shares:
archive raw JSONL under ``~/.punt-labs/quarry/sessions/``, extract session
artifacts, write a scrubbed ``.md`` capture next to the project, then POST a
``CaptureIngestRequest`` to the daemon.  Both PreCompact (mid-session) and
SessionEnd (guaranteed close) call this identically; SubagentStop calls it for
the subagent's own transcript, distinct from the parent's.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.daemon_capture import DaemonCaptureSender
from quarry.ethos_handle import EthosConfig

if TYPE_CHECKING:
    from quarry.artifacts import SessionArtifacts
    from quarry.transcript_reader import TranscriptReader

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TranscriptSource:
    """Which transcript a hook captures, and the id it is filed under.

    ``session_id`` is the identity the capture files under — the parent
    session id for PreCompact and SessionEnd, the subagent id for SubagentStop
    (that carrier decision belongs to the caller). ``cwd`` may be empty: an
    unregistered directory still archives and ingests. ``label`` names the
    producer (``"pre-compact"``, ``"session-end"``, ``"subagent-stop"``) so
    the scrub log line still identifies its origin.
    """

    cwd: str
    session_id: str
    transcript_path: Path
    label: str


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

    One instance per hook invocation.  ``agent_handle`` is the identity the
    capture is attributed to and ``summary`` the one-line description every
    chunk carries; both are empty when the caller has neither, and the daemon
    still routes to ``<repo>-captures`` either way.
    """

    __slots__ = ("_agent_handle", "_reader", "_source", "_summary")

    _source: TranscriptSource
    _reader: TranscriptReader
    _agent_handle: str
    _summary: str

    def __new__(
        cls,
        source: TranscriptSource,
        reader: TranscriptReader,
        *,
        agent_handle: str = "",
        summary: str = "",
    ) -> Self:
        self = super().__new__(cls)
        self._source = source
        # The reader is injected so a caller that already read the transcript
        # (SubagentStop, for the distilled report) shares that one parse.
        self._reader = reader
        self._agent_handle = agent_handle
        self._summary = summary
        return self

    @classmethod
    def for_session(cls, source: TranscriptSource) -> Self:
        """Build the capture for a *parent* session: attributed to the repo pin.

        The one place a parent-session hook resolves identity, so PreCompact
        and SessionEnd cannot drift. A subagent's capture must not come through
        here — its ``cwd`` is the repo and the pin would name the leader.
        """
        from quarry.transcript_reader import TranscriptReader  # noqa: PLC0415

        handle = EthosConfig.agent_handle_at(source.cwd) if source.cwd else ""
        reader = TranscriptReader(source.transcript_path)
        return cls(source, reader, agent_handle=handle)

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

        archived = self._archive()

        raw_text = self._reader.text()
        if not raw_text:
            logger.debug("%s: no conversation text found", self._source.label)
            return TranscriptCaptureOutcome(
                archived=archived, sent=False, text_captured=False
            )

        artifacts = extract_artifacts(raw_text)
        header = format_artifacts_header(artifacts)
        wire_text = f"{header}\n\n{raw_text}" if header else raw_text

        if self._source.cwd:
            iso_timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._write_local_capture(
                project_dir=Path(self._source.cwd),
                timestamp=iso_timestamp,
                artifacts=artifacts,
                text=raw_text,
            )

        sent = self._send(wire_text)
        return TranscriptCaptureOutcome(
            archived=archived, sent=sent, text_captured=True
        )

    def _archive(self) -> bool:
        """Copy the raw JSONL under sessions/ and dedup prior archives for it."""
        sessions_dir = Path.home() / ".punt-labs" / "quarry" / "sessions"
        try:
            self._reader.archive(self._source.session_id, sessions_dir)
        except OSError:
            logger.exception(
                "%s: archival failed, proceeding with ingest", self._source.label
            )
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
                session_id=self._source.session_id,
                timestamp=timestamp,
                artifacts=artifacts,
                text=text,
                label=self._source.label,
            )
        )

    def _send(self, wire_text: str) -> bool:
        """Post the wire text to the daemon; return whether the send succeeded."""
        from quarry.api import CaptureIngestRequest  # noqa: PLC0415

        request = CaptureIngestRequest(
            content=wire_text,
            cwd=self._source.cwd,
            session_id=self._source.session_id,
            agent_handle=self._agent_handle,
            summary=self._summary,
            format_hint="markdown",
        )
        unreachable = (
            f"{self._source.label}: daemon unreachable; transcript archived, "
            "run backfill-sessions to index it"
        )
        return DaemonCaptureSender().send_capture(request, unreachable_log=unreachable)
