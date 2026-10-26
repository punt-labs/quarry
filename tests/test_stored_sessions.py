"""Tests for quarry.stored_sessions: a stored session is skipped only while current."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from quarry.stored_sessions import StoredSessions
from quarry.transcript_snapshot import TranscriptSnapshot

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_NAME = "1e7aa08d-c485-45d1-8228-54d1a375c812"


def _transcript(tmp_path: Path, text: str = "{}") -> Path:
    path = tmp_path / f"{_NAME}.jsonl"
    path.write_text(text)
    return path


def _version(transcript: Path) -> str:
    return TranscriptSnapshot.taken(transcript).version


def _at(transcript: Path, version: str) -> TranscriptSnapshot:
    return TranscriptSnapshot(transcript, version)


def _docs(
    name: str = "session-1e7aa08d", *, version: object
) -> list[dict[str, object]]:
    return [{"document_name": name, "source_version": version}]


class TestTranscriptSnapshot:
    def test_it_names_the_size_and_the_nanosecond_mtime(self, tmp_path: Path) -> None:
        transcript = _transcript(tmp_path, "{}\n")
        stat = transcript.stat()
        version = _version(transcript)
        assert version == f"{stat.st_size}:{stat.st_mtime_ns}"

    def test_growth_changes_it(self, tmp_path: Path) -> None:
        transcript = _transcript(tmp_path)
        before = _version(transcript)
        with transcript.open("a") as fh:
            fh.write("{}\n")
        assert _version(transcript) != before

    def test_an_unexaminable_transcript_logs_the_class_and_the_name(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        missing = tmp_path / f"{_NAME}.jsonl"
        with caplog.at_level(logging.WARNING):
            assert _version(missing) == ""
        [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert "FileNotFoundError" in record.getMessage()
        assert "session-1e7aa08d" in record.getMessage()
        assert str(tmp_path) not in caplog.text


class TestIsCurrent:
    def test_a_session_stored_at_the_transcripts_version_is_current(
        self, tmp_path: Path
    ) -> None:
        version = _version(_transcript(tmp_path))
        transcript = tmp_path / f"{_NAME}.jsonl"
        assert StoredSessions(_docs(version=version)).is_current(
            _at(transcript, version)
        )

    def test_a_transcript_that_grew_after_its_read_is_not_current(
        self, tmp_path: Path
    ) -> None:
        """The #556 race: the row is stamped with the version read, the transcript
        grows before the row is written, so a later ingestion time proves nothing."""
        transcript = _transcript(tmp_path)
        read_at = _version(transcript)
        with transcript.open("a") as fh:
            fh.write("{}\n")
        now = _version(transcript)
        assert not StoredSessions(_docs(version=read_at)).is_current(
            _at(transcript, now)
        )

    def test_a_row_without_a_version_is_not_current(self, tmp_path: Path) -> None:
        transcript = _transcript(tmp_path)
        version = _version(transcript)
        for stored in ("", None):
            sessions = StoredSessions(_docs(version=stored))
            assert not sessions.is_current(_at(transcript, version))
        legacy = [{"document_name": "session-1e7aa08d", "ingestion_timestamp": "x"}]
        assert not StoredSessions(legacy).is_current(_at(transcript, version))

    def test_an_unexaminable_transcript_is_not_current(self, tmp_path: Path) -> None:
        transcript = tmp_path / f"{_NAME}.jsonl"
        assert not StoredSessions(_docs(version="")).is_current(_at(transcript, ""))

    def test_a_session_nothing_is_stored_for_is_not_current(
        self, tmp_path: Path
    ) -> None:
        transcript = _transcript(tmp_path)
        version = _version(transcript)
        assert not StoredSessions([]).is_current(_at(transcript, version))

    def test_a_different_session_sharing_a_prefix_is_not_a_match(
        self, tmp_path: Path
    ) -> None:
        transcript = _transcript(tmp_path)
        version = _version(transcript)
        docs = _docs("session-1e7aa08dXY", version=version)
        assert not StoredSessions(docs).is_current(_at(transcript, version))


class TestRecord:
    def test_a_session_just_stored_is_current_at_that_version(
        self, tmp_path: Path
    ) -> None:
        transcript = _transcript(tmp_path)
        version = _version(transcript)
        sessions = StoredSessions([])
        sessions.record(_at(transcript, version))
        assert sessions.is_current(_at(transcript, version))
        assert not sessions.is_current(_at(transcript, "0:0"))


class TestOnlySessionsAreRead:
    def test_another_document_is_ignored(self) -> None:
        docs = [{"document_name": "notes.md", "source_version": "1:1"}]
        assert StoredSessions(docs)._stored == {}
