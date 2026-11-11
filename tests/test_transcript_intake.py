"""Tests for TranscriptIntake — the snapshot-before-read pairing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from quarry.transcript_intake import TranscriptIntake
from quarry.transcript_reader import TranscriptReader
from quarry.transcript_snapshot import TranscriptSnapshot


def _write(path: Path, text: str) -> Path:
    message = {"role": "user", "content": [{"type": "text", "text": text}]}
    path.write_text(json.dumps({"type": "user", "message": message}))
    return path


class TestTaken:
    def test_returns_the_snapshot_and_a_working_reader(self, tmp_path: Path) -> None:
        transcript = _write(tmp_path / "s.jsonl", "hello there")
        intake = TranscriptIntake.taken(transcript)
        assert intake.snapshot.path == transcript
        assert "hello there" in intake.reader.text()

    def test_snapshots_before_the_reader_parses_the_file(self, tmp_path: Path) -> None:
        """The whole point: the stat must run before the growth, not after.

        Appending to the file inside ``TranscriptSnapshot.taken`` (the earliest
        a concurrent writer could land) must show up in the reader's own text,
        proving the stat ran first and the read second — the opposite order
        would tag a pre-growth read with a post-growth version.
        """
        transcript = _write(tmp_path / "s.jsonl", "first turn")
        real_taken = TranscriptSnapshot.taken

        def taken_then_grow(path: Path) -> TranscriptSnapshot:
            snapshot = real_taken(path)
            extra = json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "second turn"}],
                    },
                }
            )
            with path.open("a") as f:
                f.write(f"\n{extra}")
            return snapshot

        with patch(
            "quarry.transcript_snapshot.TranscriptSnapshot.taken",
            side_effect=taken_then_grow,
        ):
            intake = TranscriptIntake.taken(transcript)

        assert "second turn" in intake.reader.text()

    def test_snapshot_version_is_taken_before_the_growth(self, tmp_path: Path) -> None:
        """The returned snapshot must describe the file as it was, pre-growth.

        If the snapshot instead reflected the grown file, a later, now-
        quiescent capture at that same size would read as "unchanged" and be
        skipped — silently dropping the growth this capture already sent.
        """
        transcript = _write(tmp_path / "s.jsonl", "first turn")
        pre_growth_version = TranscriptSnapshot.taken(transcript).version

        real_taken = TranscriptSnapshot.taken

        def taken_then_grow(path: Path) -> TranscriptSnapshot:
            snapshot = real_taken(path)
            with path.open("a") as f:
                f.write("\nmore bytes after the snapshot")
            return snapshot

        with patch(
            "quarry.transcript_snapshot.TranscriptSnapshot.taken",
            side_effect=taken_then_grow,
        ):
            intake = TranscriptIntake.taken(transcript)

        assert intake.snapshot.version == pre_growth_version

    def test_the_reader_is_a_real_transcript_reader(self, tmp_path: Path) -> None:
        transcript = _write(tmp_path / "s.jsonl", "hello there")
        intake = TranscriptIntake.taken(transcript)
        assert isinstance(intake.reader, TranscriptReader)
