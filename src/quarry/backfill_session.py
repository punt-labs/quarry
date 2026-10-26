"""One Claude Code transcript, prepared for filing by backfill."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.artifacts import extract_artifacts, format_artifacts_header
from quarry.capture import CaptureRequest
from quarry.transcript import Transcript

if TYPE_CHECKING:
    from quarry.artifacts import SessionArtifacts


@final
class BackfillSession:
    """A transcript's document name, its body with the artifact header, its capture.

    The name is the stable ``session-<id[:8]>`` the compaction hook files under
    too, with no mtime in it, so one document stands for a session whichever
    writer filed it first.
    """

    __slots__ = ("_artifacts", "_project_path", "_text", "_transcript")

    _artifacts: SessionArtifacts
    _project_path: str
    _text: str
    _transcript: Path

    def __new__(cls, transcript: Path, project_path: str, text: str) -> Self:
        self = super().__new__(cls)
        self._transcript = transcript
        self._project_path = project_path
        self._text = text
        self._artifacts = extract_artifacts(text)
        return self

    @property
    def name(self) -> str:
        """The document name the session is filed under."""
        return Transcript(self._transcript).document_name()

    @property
    def body(self) -> str:
        """The text to ingest: the artifact header (if any), then the conversation."""
        header = format_artifacts_header(self._artifacts)
        return f"{header}\n\n{self._text}" if header else self._text

    def capture_request(self) -> CaptureRequest:
        """Return the request that writes the session's capture file.

        The shared CaptureWriter scrubs secrets, PII, and profanity before any
        bytes reach the git-tracked capture file.
        """
        return CaptureRequest(
            project_dir=Path(self._project_path),
            session_id=self._transcript.stem,
            timestamp=Transcript(self._transcript).timestamp("%Y-%m-%dT%H:%M:%SZ"),
            artifacts=self._artifacts,
            text=self._text,
            label="backfill",
        )
