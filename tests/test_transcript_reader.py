"""Tests for TranscriptReader — transcript text extraction and archival."""

from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.transcript_reader import TranscriptReader
from quarry.transcript_records import TranscriptRecords

if TYPE_CHECKING:
    import pytest


class TestText:
    def _write_transcript(self, path: Path, records: list[dict[str, object]]) -> None:
        lines = [json.dumps(r) for r in records]
        path.write_text("\n".join(lines))

    def test_extracts_user_and_assistant_text(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        self._write_transcript(
            transcript,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Hello"}],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Hi there"}],
                    },
                },
            ],
        )
        text = TranscriptReader(transcript).text()
        assert "[user] Hello" in text
        assert "[assistant] Hi there" in text

    def test_skips_non_conversation_records(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        self._write_transcript(
            transcript,
            [
                {"type": "file-history-snapshot", "snapshot": {}},
                {"type": "system", "message": {"role": "system"}},
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Real"}],
                    },
                },
            ],
        )
        text = TranscriptReader(transcript).text()
        assert "[user] Real" in text
        assert "snapshot" not in text
        assert "system" not in text

    def test_skips_tool_use_blocks(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        self._write_transcript(
            transcript,
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Bash"},
                            {"type": "text", "text": "Done"},
                        ],
                    },
                },
            ],
        )
        text = TranscriptReader(transcript).text()
        assert "[assistant] Done" in text
        assert "Bash" not in text

    def test_returns_empty_for_nonexistent_file(self) -> None:
        assert TranscriptReader(Path("/nonexistent/path.jsonl")).text() == ""

    def test_returns_empty_for_empty_file(self, tmp_path: Path) -> None:
        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("")
        assert TranscriptReader(transcript).text() == ""

    def test_respects_char_limit(self, tmp_path: Path) -> None:
        transcript = tmp_path / "big.jsonl"
        big_text = "a" * 200
        records = [
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": big_text}],
                    },
                }
            )
            for _ in range(5000)
        ]
        transcript.write_text("\n".join(records))

        text = TranscriptReader(transcript).text()
        # Separators add "\n\n" between entries, so allow a small margin.
        assert len(text) < TranscriptReader._MAX_CHARS * 1.02

    def test_front_truncation_keeps_newest(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        half = TranscriptReader._MAX_CHARS // 2
        msg1_text = "A" * half
        msg2_text = "B" * half
        msg3_text = "C" * 50
        self._write_transcript(
            transcript,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": msg1_text}],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": msg2_text}],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": msg3_text}],
                    },
                },
            ],
        )
        text = TranscriptReader(transcript).text()
        # First (oldest) message is dropped; the two newest are kept.
        assert "A" * 50 not in text
        assert msg2_text in text
        assert msg3_text in text

    def test_returns_empty_for_unreadable_file(self, tmp_path: Path) -> None:
        transcript = tmp_path / "binary.jsonl"
        transcript.write_bytes(b"\x80\x81\x82\xff\xfe")
        assert TranscriptReader(transcript).text() == ""


def _turn(role: str, text: str) -> dict[str, object]:
    return {
        "type": role,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _write(path: Path, *records: object) -> TranscriptReader:
    path.write_text("\n".join(json.dumps(r) for r in records))
    return TranscriptReader(path)


class TestMalformedLines:
    """Every extraction reads through one record iterator that skips junk."""

    def test_non_object_lines_are_skipped_in_order(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(_turn("user", "first")),
                    "not json",
                    "[1, 2]",
                    json.dumps(_turn("assistant", "second")),
                ]
            )
        )
        reader = TranscriptReader(path)
        assert reader.text() == "[user] first\n\n[assistant] second"
        assert reader.last_assistant_text() == "second"

    def test_a_line_nested_too_deeply_to_parse_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        deep = "[" * 200_000
        path.write_text(f"{deep}\n{json.dumps(_turn('user', 'after'))}")

        assert TranscriptReader(path).text() == "[user] after"


class TestTruncation:
    """What the bounds drop is reported, never silent."""

    @staticmethod
    def _transcript(path: Path, count: int) -> Path:
        path.write_text(
            "\n".join(json.dumps(_turn("user", f"m{n}")) for n in range(count))
        )
        return path

    def test_the_record_bound_drop_is_a_warning_naming_the_transcript(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = self._transcript(tmp_path / "session.jsonl", 8)

        with (
            patch.object(TranscriptRecords, "MAX", 5),
            patch.object(TranscriptRecords, "TRIM", 2),
            caplog.at_level(logging.WARNING, logger="quarry.transcript_reader"),
        ):
            reader = TranscriptReader(path)

        assert reader.truncated == 3
        [record] = caplog.records
        assert "session.jsonl" in record.getMessage()
        assert "3 oldest records" in record.getMessage()

    def test_a_transcript_within_the_bound_is_not_truncated(
        self, tmp_path: Path
    ) -> None:
        reader = TranscriptReader(self._transcript(tmp_path / "t.jsonl", 3))

        assert reader.truncated == 0

    def test_the_character_budget_drop_is_logged_at_info_with_its_count(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        reader = TranscriptReader(self._transcript(tmp_path / "t.jsonl", 4))

        with (
            patch.object(TranscriptReader, "_MAX_CHARS", 20),
            caplog.at_level(logging.INFO, logger="quarry.transcript_reader"),
        ):
            reader.text()

        [record] = caplog.records
        assert record.levelno == logging.INFO
        assert "dropped" in record.getMessage()


class TestReadOnce:
    """A reader parses its file at construction and never again."""

    def test_extractions_answer_from_the_one_read(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        reader = _write(path, _turn("user", "asked"), _turn("assistant", "answered"))
        path.unlink()
        assert reader.text() == "[user] asked\n\n[assistant] answered"
        assert reader.last_assistant_text() == "answered"


class TestLastAssistantText:
    def test_returns_the_final_assistant_turn_without_prefix(
        self, tmp_path: Path
    ) -> None:
        reader = _write(
            tmp_path / "t.jsonl",
            _turn("assistant", "first"),
            _turn("user", "more"),
            _turn("assistant", "  final report  "),
        )
        assert reader.last_assistant_text() == "final report"

    def test_skips_a_trailing_tool_only_turn(self, tmp_path: Path) -> None:
        tool_only: dict[str, object] = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Bash"}],
            },
        }
        reader = _write(tmp_path / "t.jsonl", _turn("assistant", "real"), tool_only)
        assert reader.last_assistant_text() == "real"

    def test_no_assistant_turn_is_empty(self, tmp_path: Path) -> None:
        reader = _write(tmp_path / "t.jsonl", _turn("user", "alone"))
        assert reader.last_assistant_text() == ""

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert TranscriptReader(tmp_path / "nope.jsonl").last_assistant_text() == ""


class TestArchive:
    def _make_transcript(self, tmp_path: Path) -> Path:
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('{"type":"user"}\n')
        return transcript

    def test_copies_raw_jsonl_to_sessions_dir(self, tmp_path: Path) -> None:
        transcript = self._make_transcript(tmp_path)
        sessions_dir = tmp_path / "sessions"

        TranscriptReader(transcript).archive("abc12345-full-id", sessions_dir)

        archived = list(sessions_dir.glob("session-abc12345-*.jsonl"))
        assert len(archived) == 1
        assert archived[0].read_text() == transcript.read_text()

    def test_deduplicates_prior_session_archives(self, tmp_path: Path) -> None:
        transcript = self._make_transcript(tmp_path)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        prior = sessions_dir / "session-abc12345-20260224T100000.jsonl"
        prior.write_text("{}\n")

        TranscriptReader(transcript).archive("abc12345-full-id", sessions_dir)

        assert not prior.exists()
        assert len(list(sessions_dir.glob("session-abc12345-*.jsonl"))) == 1

    def test_prunes_files_past_retention(self, tmp_path: Path) -> None:
        transcript = self._make_transcript(tmp_path)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        old_file = sessions_dir / "session-oldoldol-20250101T000000.jsonl"
        old_file.write_text("{}\n")
        old_mtime = old_file.stat().st_mtime - (100 * 86400)
        os.utime(old_file, (old_mtime, old_mtime))

        TranscriptReader(transcript).archive("abc12345-full-id", sessions_dir)

        assert not old_file.exists()
        assert len(list(sessions_dir.glob("session-abc12345-*.jsonl"))) == 1

    def test_copy_failure_propagates(self, tmp_path: Path) -> None:
        """A copy failure raises — the hook handles it, the reader stays honest."""
        transcript = self._make_transcript(tmp_path)
        sessions_dir = tmp_path / "sessions"

        with patch(
            "quarry.transcript_reader.shutil.copy",
            side_effect=OSError("disk full"),
        ):
            try:
                TranscriptReader(transcript).archive("abc12345", sessions_dir)
            except OSError:
                pass
            else:
                msg = "expected archive to propagate the copy failure"
                raise AssertionError(msg)


class TestAReadFaultIsNotAnEmptyConversation:
    @staticmethod
    def _failing_read(monkeypatch: pytest.MonkeyPatch) -> None:
        def emfile(self: Path, *args: object, **kwargs: object) -> str:
            raise OSError(errno.EMFILE, "Too many open files")

        monkeypatch.setattr(Path, "read_text", emfile)

    def test_the_default_reader_still_reads_it_as_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transcript = tmp_path / "session.jsonl"
        transcript.write_text("{}")
        self._failing_read(monkeypatch)

        assert TranscriptReader(transcript).text() == ""

    def test_a_reader_built_from_text_parses_that_text_and_never_reads_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transcript = tmp_path / "gone.jsonl"  # no file: only the text is used
        self._failing_read(monkeypatch)
        record = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
        }

        reader = TranscriptReader.from_text(transcript, json.dumps(record))

        assert "Hi" in reader.text()
