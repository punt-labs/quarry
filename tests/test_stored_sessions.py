"""Tests for quarry.stored_sessions: a stored session is skipped only while current."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from quarry.stored_sessions import StoredSessions

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_NAME = "1e7aa08d-c485-45d1-8228-54d1a375c812"


def _transcript(tmp_path: Path, *, modified: datetime) -> Path:
    path = tmp_path / f"{_NAME}.jsonl"
    path.write_text("{}")
    stamp = modified.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _docs(name: str = "session-1e7aa08d", *, stored: str) -> list[dict[str, object]]:
    return [{"document_name": name, "ingestion_timestamp": stored}]


_NOON = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class TestIsCurrent:
    def test_a_session_stored_after_its_last_write_is_current(
        self, tmp_path: Path
    ) -> None:
        transcript = _transcript(tmp_path, modified=_NOON)
        stored = (_NOON + timedelta(minutes=1)).isoformat()
        assert StoredSessions(_docs(stored=stored)).is_current(transcript)

    def test_a_session_stored_at_its_last_write_is_current(
        self, tmp_path: Path
    ) -> None:
        transcript = _transcript(tmp_path, modified=_NOON)
        assert StoredSessions(_docs(stored=_NOON.isoformat())).is_current(transcript)

    def test_a_transcript_written_after_its_document_has_grown(
        self, tmp_path: Path
    ) -> None:
        transcript = _transcript(tmp_path, modified=_NOON + timedelta(minutes=5))
        assert not StoredSessions(_docs(stored=_NOON.isoformat())).is_current(
            transcript
        )

    def test_a_session_nothing_is_stored_for_is_not_current(
        self, tmp_path: Path
    ) -> None:
        transcript = _transcript(tmp_path, modified=_NOON)
        assert not StoredSessions([]).is_current(transcript)

    def test_a_different_session_sharing_a_prefix_is_not_a_match(
        self, tmp_path: Path
    ) -> None:
        transcript = _transcript(tmp_path, modified=_NOON)
        docs = _docs("session-1e7aa08dXY", stored=_NOON.isoformat())
        assert not StoredSessions(docs).is_current(transcript)

    def test_a_timestamp_without_an_offset_is_read_as_utc(self, tmp_path: Path) -> None:
        transcript = _transcript(tmp_path, modified=_NOON)
        stored = (_NOON + timedelta(minutes=1)).replace(tzinfo=None).isoformat()
        assert StoredSessions(_docs(stored=stored)).is_current(transcript)

    def test_a_z_suffixed_timestamp_is_read(self, tmp_path: Path) -> None:
        transcript = _transcript(tmp_path, modified=_NOON)
        assert StoredSessions(_docs(stored="2026-01-01T12:01:00Z")).is_current(
            transcript
        )

    def test_an_unreadable_timestamp_is_not_current_and_says_so(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        transcript = _transcript(tmp_path, modified=_NOON)
        with caplog.at_level(logging.WARNING):
            sessions = StoredSessions(_docs(stored="yesterday"))
        assert not sessions.is_current(transcript)
        assert "ValueError" in caplog.text
        assert "session-1e7aa08d" in caplog.text

    def test_a_transcript_that_cannot_be_examined_is_not_current(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / f"{_NAME}.jsonl"
        assert not StoredSessions(_docs(stored=_NOON.isoformat())).is_current(missing)


class TestRecord:
    def test_a_session_just_stored_is_current(self, tmp_path: Path) -> None:
        now = datetime.now(tz=UTC)
        transcript = _transcript(tmp_path, modified=now - timedelta(seconds=1))
        sessions = StoredSessions([])
        assert not sessions.is_current(transcript)
        sessions.record(transcript, now)
        assert sessions.is_current(transcript)


class TestOnlySessionsAreRead:
    def test_another_documents_timestamp_is_not_parsed_or_complained_about(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        docs = [{"document_name": "notes.md", "ingestion_timestamp": "garbage"}]
        with caplog.at_level(logging.WARNING):
            StoredSessions(docs)
        assert caplog.text == ""


class TestAnUnexaminableTranscript:
    def test_it_logs_one_warning_with_the_class_and_the_name(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        missing = tmp_path / f"{_NAME}.jsonl"
        sessions = StoredSessions(_docs(stored=_NOON.isoformat()))
        with caplog.at_level(logging.WARNING):
            assert not sessions.is_current(missing)
        [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert "FileNotFoundError" in record.getMessage()
        assert "session-1e7aa08d" in record.getMessage()
        assert str(tmp_path) not in caplog.text


class TestRecordStoresTheSnapshot:
    def test_a_transcript_written_after_the_snapshot_is_not_current(
        self, tmp_path: Path
    ) -> None:
        snapshot = datetime.now(UTC) - timedelta(minutes=5)
        # Written after the read but before the store was recorded.
        transcript = _transcript(tmp_path, modified=snapshot + timedelta(minutes=1))
        stored = StoredSessions([])
        stored.record(transcript, snapshot)
        assert not stored.is_current(transcript)

    def test_a_transcript_untouched_since_the_snapshot_is_current(
        self, tmp_path: Path
    ) -> None:
        snapshot = datetime.now(UTC)
        transcript = _transcript(tmp_path, modified=snapshot - timedelta(minutes=1))
        stored = StoredSessions([])
        stored.record(transcript, snapshot)
        assert stored.is_current(transcript)
