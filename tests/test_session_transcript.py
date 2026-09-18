"""Behaviour of :class:`quarry.session_transcript.SessionTranscriptCapture`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import pytest

from quarry.session_transcript import SessionTranscriptCapture, TranscriptSource

if TYPE_CHECKING:
    from quarry.transcript_reader import TranscriptReader


def _source(cwd: str = "", path: Path = Path("/dev/null")) -> TranscriptSource:
    return TranscriptSource(cwd, "s1", path, "test")


def _capture() -> SessionTranscriptCapture:
    return SessionTranscriptCapture(_source())


def _write_transcript(path: Path, text: str) -> Path:
    message = {"role": "user", "content": [{"type": "text", "text": text}]}
    path.write_text(json.dumps({"type": "user", "message": message}))
    return path


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


class TestForSession:
    """The one place a parent-session hook resolves identity."""

    def test_resolves_the_repo_pin_for_a_cwd(self, tmp_path: Path) -> None:
        with patch(
            "quarry.ethos_handle.EthosConfig.agent_handle_at", return_value="claude"
        ) as pin:
            capture = SessionTranscriptCapture.for_session(_source(str(tmp_path)))
        pin.assert_called_once_with(str(tmp_path))
        assert capture._agent_handle == "claude"

    def test_empty_cwd_skips_the_pin_lookup(self) -> None:
        with patch("quarry.ethos_handle.EthosConfig.agent_handle_at") as pin:
            capture = SessionTranscriptCapture.for_session(_source(""))
        pin.assert_not_called()
        assert capture._agent_handle == ""


class TestWirePayload:
    def test_summary_and_handle_ride_the_capture_request(self, tmp_path: Path) -> None:
        transcript = _write_transcript(tmp_path / "s.jsonl", "hello there")
        capture = SessionTranscriptCapture(
            _source(path=transcript), agent_handle="rmh", summary="one line"
        )
        with patch(
            "quarry.daemon_capture.DaemonCaptureSender.send_capture", return_value=True
        ) as sender:
            outcome = capture.capture()
        assert outcome.text_captured is True
        req = sender.call_args[0][0]
        assert req.agent_handle == "rmh"
        assert req.summary == "one line"
        assert req.session_id == "s1"
        assert "hello there" in req.content

    def test_empty_transcript_sends_nothing(self, tmp_path: Path) -> None:
        empty = tmp_path / "e.jsonl"
        empty.write_text("")
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as sender:
            outcome = SessionTranscriptCapture(_source(path=empty)).capture()
        assert outcome.text_captured is False
        assert outcome.sent is False
        sender.assert_not_called()
