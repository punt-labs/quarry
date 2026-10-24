"""Behaviour of :class:`quarry.session_transcript.SessionTranscriptCapture`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from quarry.send_outcome import SendOutcome
from quarry.session_transcript import SessionTranscriptCapture, TranscriptSource
from quarry.transcript_reader import TranscriptReader


def _source(cwd: str = "", path: Path = Path("/dev/null")) -> TranscriptSource:
    return TranscriptSource(cwd, "s1", path, "test")


def _capture_with(reader: MagicMock) -> SessionTranscriptCapture:
    return SessionTranscriptCapture(_source(), cast("TranscriptReader", reader))


def _write_transcript(path: Path, text: str) -> Path:
    message = {"role": "user", "content": [{"type": "text", "text": text}]}
    path.write_text(json.dumps({"type": "user", "message": message}))
    return path


class TestArchiveExceptionNarrowing:
    """``_archive`` catches only ``OSError`` — a programmer error must surface."""

    def test_oserror_is_caught_and_reported_as_failure(self) -> None:
        reader = MagicMock()
        reader.archive.side_effect = FileNotFoundError("gone")
        assert _capture_with(reader)._archive() is False

    def test_non_oserror_propagates(self) -> None:
        reader = MagicMock()
        reader.archive.side_effect = ValueError("not an I/O failure")
        with pytest.raises(ValueError, match="not an I/O failure"):
            _capture_with(reader)._archive()


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

    def test_reads_the_source_transcript(self, tmp_path: Path) -> None:
        transcript = _write_transcript(tmp_path / "s.jsonl", "from the source")
        capture = SessionTranscriptCapture.for_session(_source(path=transcript))
        assert "from the source" in capture._reader.text()


class TestWirePayload:
    def test_summary_and_handle_ride_the_capture_request(self, tmp_path: Path) -> None:
        transcript = _write_transcript(tmp_path / "s.jsonl", "hello there")
        capture = SessionTranscriptCapture(
            _source(path=transcript),
            TranscriptReader(transcript),
            agent_handle="rmh",
            summary="one line",
        )
        with patch(
            "quarry.daemon_capture.DaemonCaptureSender.send_capture",
            return_value=SendOutcome(sent=True),
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
            outcome = SessionTranscriptCapture(
                _source(path=empty), TranscriptReader(empty)
            ).capture()
        assert outcome.text_captured is False
        assert outcome.sent is False
        sender.assert_not_called()


class TestUnreachableText:
    """The unreachable log must not claim an archive that was not made."""

    @staticmethod
    def _log_for(tmp_path: Path, *, archive_ok: bool) -> str:
        reader = MagicMock()
        reader.text.return_value = "hello"
        if not archive_ok:
            reader.archive.side_effect = OSError
        source = TranscriptSource("", "sid", tmp_path / "t.jsonl", "pre-compact")
        with patch(
            "quarry.daemon_capture.DaemonCaptureSender.send_capture",
            return_value=SendOutcome.unreachable("x"),
        ) as send:
            SessionTranscriptCapture(source, reader).capture()
        return str(send.call_args.kwargs["unreachable_log"])

    def test_an_archived_transcript_is_named_with_the_backfill_advice(
        self, tmp_path: Path
    ) -> None:
        log = self._log_for(tmp_path, archive_ok=True)
        assert "transcript archived" in log
        assert "backfill-sessions" in log

    def test_a_transcript_that_could_not_be_archived_says_so(
        self, tmp_path: Path
    ) -> None:
        log = self._log_for(tmp_path, archive_ok=False)
        assert "could not be archived" in log
        assert "backfill" not in log
