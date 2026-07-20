"""Tests for TranscriptReader — transcript text extraction and archival."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from quarry.transcript_reader import TranscriptReader


class TestMessageText:
    def test_extracts_short_tool_result_string(self) -> None:
        record: dict[str, object] = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": "5 passed, 0 failed",
                    }
                ],
            },
        }
        result = TranscriptReader.message_text(record)
        assert result is not None
        assert "[tool_result] 5 passed, 0 failed" in result

    def test_extracts_short_tool_result_list(self) -> None:
        record: dict[str, object] = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": [{"type": "text", "text": "No matches found"}],
                    }
                ],
            },
        }
        result = TranscriptReader.message_text(record)
        assert result is not None
        assert "[tool_result] No matches found" in result

    def test_skips_long_tool_result(self) -> None:
        long_output = "x" * 501
        record: dict[str, object] = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": long_output,
                    }
                ],
            },
        }
        assert TranscriptReader.message_text(record) is None

    def test_skips_tool_use_blocks(self) -> None:
        record: dict[str, object] = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_abc",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    }
                ],
            },
        }
        assert TranscriptReader.message_text(record) is None

    def test_returns_none_for_non_message_record(self) -> None:
        assert TranscriptReader.message_text({"type": "file-history-snapshot"}) is None


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
