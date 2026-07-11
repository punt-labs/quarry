"""Tests for the Transcript value class."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from quarry.transcript import Transcript


def test_session_prefix_is_first_eight_chars_of_stem() -> None:
    t = Transcript(Path("/tmp/1e7aa08d-9f21-4c.jsonl"))
    assert t.session_prefix == "1e7aa08d"


def test_document_name_format(tmp_path: Path) -> None:
    transcript = tmp_path / "sess1234abcd.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    name = Transcript(transcript).document_name()
    assert name.startswith("session-sess1234-")


def test_timestamp_uses_file_mtime(tmp_path: Path) -> None:
    transcript = tmp_path / "sess.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    expected = datetime.fromtimestamp(transcript.stat().st_mtime, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert Transcript(transcript).timestamp("%Y-%m-%dT%H:%M:%SZ") == expected


def test_timestamp_degrades_to_now_when_file_missing(tmp_path: Path) -> None:
    """A vanished transcript makes stat() raise OSError — fall back to now()."""
    missing = tmp_path / "gone.jsonl"  # never created
    before = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    stamp = Transcript(missing).timestamp("%Y%m%dT%H%M%S")
    after = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    assert before[:8] <= stamp[:8] <= after[:8]
    assert len(stamp) == len("YYYYMMDDTHHMMSS")
