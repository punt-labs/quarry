"""Behaviour of :class:`quarry.session_transcript.SessionTranscriptCapture`."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest

from quarry.session_transcript import SessionTranscriptCapture

if TYPE_CHECKING:
    from quarry.transcript_reader import TranscriptReader


def _capture() -> SessionTranscriptCapture:
    return SessionTranscriptCapture(
        cwd="",
        session_id="s1",
        transcript_path=Path("/dev/null"),
        label="test",
    )


class TestArchiveExceptionNarrowing:
    """``_archive`` catches only ``OSError`` — a programmer error must surface."""

    def test_oserror_is_caught_and_reported_as_failure(self) -> None:
        reader = MagicMock()
        reader.archive.side_effect = FileNotFoundError("gone")
        assert _capture()._archive(cast("TranscriptReader", reader)) is False

    def test_non_oserror_propagates(self) -> None:
        reader = MagicMock()
        reader.archive.side_effect = ValueError("not an I/O failure")
        with pytest.raises(ValueError, match="not an I/O failure"):
            _capture()._archive(cast("TranscriptReader", reader))
