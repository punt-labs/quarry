"""Behaviour of :class:`quarry.session_transcript.SessionTranscriptCapture`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from quarry.api import CaptureIngestRequest, TaskAccepted
from quarry.client import QuarryClient
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

    def test_the_request_carries_the_instant_the_transcript_was_read(
        self, tmp_path: Path
    ) -> None:
        from datetime import UTC, datetime

        transcript = _write_transcript(tmp_path / "s.jsonl", "hello there")
        capture = SessionTranscriptCapture(
            _source(path=transcript), TranscriptReader(transcript)
        )
        before = datetime.now(UTC)
        with patch(
            "quarry.daemon_capture.DaemonCaptureSender.send_capture",
            return_value=SendOutcome(sent=True),
        ) as sender:
            capture.capture()
        after = datetime.now(UTC)
        req = sender.call_args[0][0]
        assert before <= datetime.fromisoformat(req.captured_at) <= after

    def test_the_snapshot_is_taken_before_the_transcript_is_read(
        self, tmp_path: Path
    ) -> None:
        from datetime import UTC, datetime

        seen: list[datetime] = []

        def read() -> str:
            seen.append(datetime.now(UTC))
            return "hello"

        reader = MagicMock()
        reader.text.side_effect = read
        capture = SessionTranscriptCapture(
            _source(path=tmp_path), cast("TranscriptReader", reader)
        )
        with patch(
            "quarry.daemon_capture.DaemonCaptureSender.send_capture",
            return_value=SendOutcome(sent=True),
        ) as sender:
            capture.capture()
        assert datetime.fromisoformat(sender.call_args[0][0].captured_at) <= seen[0]

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


class TestAnOversizedMessageIsNeverEmptyText:
    """The hook paths keep the tail of a message over the budget, not nothing."""

    def test_one_message_over_the_budget_is_captured_from_its_tail(
        self, tmp_path: Path
    ) -> None:
        budget = TranscriptReader._MAX_CHARS
        big = _write_transcript(tmp_path / "big.jsonl", "a" * budget + "TAIL")
        with patch(
            "quarry.daemon_capture.DaemonCaptureSender.send_capture",
            return_value=SendOutcome(sent=True),
        ) as sender:
            outcome = SessionTranscriptCapture(
                _source(path=big), TranscriptReader(big)
            ).capture()

        assert outcome.text_captured is True
        assert sender.call_args[0][0].content.endswith("TAIL")


class TestSnapshotOrderingAgainstAGrowingFile:
    """quarry-uq95 round 3: the version must be taken BEFORE the file is read.

    ``TranscriptReader.__new__`` reads the file synchronously at construction.
    A snapshot taken any later than that read can describe a file a writer
    already grew past the content this capture actually sends — the stored
    row is then tagged with a version the NEXT, now-quiescent capture already
    matches, so that capture is skipped and the growth it would have ingested
    is silently lost forever.

    Reproduces the race by making the file grow the instant
    ``TranscriptSnapshot.taken`` is invoked: the unfixed code calls it
    AFTER building the reader (so the growth already landed by the time the
    snapshot is taken, and the stale, pre-growth text ends up tagged with the
    post-growth version); the fixed code calls it BEFORE building the reader
    (so the snapshot describes the pre-growth file, and the reader's own read
    -- whatever it sees -- is tagged honestly).
    """

    @staticmethod
    def _capture_once(
        transcript: Path, client: QuarryClient, *, grow_on_snapshot: str | None
    ) -> None:
        """Run one real ``for_session().capture()``, then await its task.

        *grow_on_snapshot*, when given, is appended to the transcript the
        instant ``TranscriptSnapshot.taken`` runs -- the earliest point a
        real writer's append could land relative to this capture's own
        read/stat pair.  ``capture()`` fires the daemon POST and returns
        before the background store finishes (the production fire-and-forget
        contract), so the real task id is captured off the wrapped client and
        awaited to a terminal state before this returns.
        """
        from quarry.transcript_snapshot import TranscriptSnapshot

        real_taken = TranscriptSnapshot.taken

        def taken_and_maybe_grow(path: Path) -> TranscriptSnapshot:
            if grow_on_snapshot is not None:
                with path.open("a") as f:
                    f.write(grow_on_snapshot)
            return real_taken(path)

        accepted_ids: list[str] = []
        real_capture = client.capture

        def capture_and_record(
            req: CaptureIngestRequest, *, timeout: float | None = None
        ) -> TaskAccepted:
            accepted = real_capture(req, timeout=timeout)
            accepted_ids.append(accepted.task_id)
            return accepted

        source = TranscriptSource("", "abcd1234ef", transcript, "test")
        with (
            patch(
                "quarry.transcript_snapshot.TranscriptSnapshot.taken",
                side_effect=taken_and_maybe_grow,
            ),
            patch.object(client, "capture", side_effect=capture_and_record),
        ):
            outcome = SessionTranscriptCapture.for_session(source).capture()
        assert outcome.sent, outcome
        task = client.await_task(accepted_ids[0])
        assert task.is_completed, task

    def test_growth_between_the_read_and_the_stat_is_not_permanently_lost(
        self, tmp_path: Path
    ) -> None:
        from quarry.api import ShowRequest
        from tests.inproc_daemon import InProcessDaemon

        transcript = _write_transcript(tmp_path / "s.jsonl", "first turn content")
        daemon = InProcessDaemon(tmp_path / "daemon")

        with (
            daemon.client() as client,
            patch("quarry.client.TargetResolver.connect", return_value=client),
        ):
            # Pass 1: the file grows by one line the instant the snapshot is
            # taken (relative to this capture's own read -- the earliest a
            # concurrent writer's append could land under the buggy ordering,
            # where the stat runs after the reader has already parsed the
            # file).
            grown_line = json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "second turn, appended mid-capture",
                            }
                        ],
                    },
                }
            )
            self._capture_once(transcript, client, grow_on_snapshot=f"\n{grown_line}")

            # Pass 2: the file is now quiescent at its grown size. If pass 1's
            # stored version already describes this grown size (the bug), the
            # gate sees "unchanged" and skips -- the second turn is never
            # ingested on ANY pass.
            self._capture_once(transcript, client, grow_on_snapshot=None)

            info = client.show_document(
                ShowRequest(document="session-abcd1234", collection="default-captures")
            )
            text = "\n".join(
                client.show_page(
                    ShowRequest(
                        document="session-abcd1234",
                        collection="default-captures",
                        page=page,
                    )
                ).text
                for page in range(1, info.total_pages + 1)
            )
        assert "second turn, appended mid-capture" in text, (
            "the growth written between the read and the stat was never "
            "ingested by either pass -- the version gate matched it as "
            "already-stored and silently dropped it for good"
        )


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
