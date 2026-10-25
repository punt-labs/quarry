"""A capture's snapshot time: validated at the route, stored as ingestion time.

A transcript is read by the hook, then queued for the daemon.  The stored
``ingestion_timestamp`` is the instant the hook read its snapshot, so a transcript
written after that read is newer than its document and backfill ingests it again.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from quarry.capture_snapshot import CaptureSnapshot
from quarry.config import Settings
from quarry.daemon.content_jobs import ScrubbedIngestJob
from quarry.daemon.ingest_labels import IngestLabels
from quarry.db.facade import Database
from quarry.stored_sessions import StoredSessions
from tests.fakes import FakeEmbeddingBackend, patched_embedder

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from quarry.daemon.context import DaemonContext

_NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


class TestParse:
    def test_absent_is_none(self) -> None:
        assert CaptureSnapshot.parse("", now=_NOW) is None

    def test_a_utc_instant_is_read(self) -> None:
        raw = "2026-10-24T11:59:00Z"
        assert CaptureSnapshot.parse(raw, now=_NOW) == _NOW - timedelta(minutes=1)

    def test_an_offset_is_converted_to_utc(self) -> None:
        raw = "2026-10-24T13:00:00+02:00"
        assert CaptureSnapshot.parse(raw, now=_NOW) == _NOW - timedelta(hours=1)

    @pytest.mark.parametrize(
        "raw",
        ["yesterday", "2026-10-24", "2026-10-24T11:59:00", "2026-13-01T00:00:00Z"],
    )
    def test_anything_that_is_not_an_aware_iso_instant_is_refused(
        self, raw: str
    ) -> None:
        with pytest.raises(ValueError, match="captured_at"):
            CaptureSnapshot.parse(raw, now=_NOW)

    def test_the_future_is_clamped_to_now(self) -> None:
        raw = "2026-10-24T13:00:00Z"
        assert CaptureSnapshot.parse(raw, now=_NOW) == _NOW

    def test_a_clamp_is_logged_at_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger="quarry.capture_snapshot"):
            CaptureSnapshot.parse("2026-10-24T13:00:00Z", now=_NOW)
        assert [r.levelno for r in caplog.records] == [logging.DEBUG]

    @pytest.mark.parametrize(
        "raw",
        ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"],
        ids=["lower-edge", "upper-edge"],
    )
    def test_a_value_at_the_edge_of_the_calendar_is_a_value_error(
        self, raw: str
    ) -> None:
        with pytest.raises(ValueError, match="captured_at"):
            CaptureSnapshot.parse(raw, now=_NOW)

    def test_an_implausibly_old_instant_is_refused(self) -> None:
        with pytest.raises(ValueError, match="too old"):
            CaptureSnapshot.parse("1999-12-31T23:59:59Z", now=_NOW)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    path = tmp_path / "lancedb"
    path.mkdir()
    with patched_embedder(FakeEmbeddingBackend(768)):
        yield Database.connect(path)


def _job(captured_at: datetime | None) -> ScrubbedIngestJob:
    return ScrubbedIngestJob(
        name="session-abcd1234",
        content="[user] hello there, this is the captured conversation text",
        labels=IngestLabels(
            overwrite=True,
            collection="r-captures",
            agent_handle="",
            memory_type="",
            summary="",
        ),
        format_hint="markdown",
        scrub_label="capture",
        captured_at=captured_at,
    )


def _store(db: Database, captured_at: datetime | None) -> None:
    ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
    with patched_embedder(FakeEmbeddingBackend(768)):
        _job(captured_at).scrub_and_ingest(ctx)


class TestTheStoredTimestamp:
    def test_it_is_the_snapshot_time_when_one_is_given(self, db: Database) -> None:
        snapshot = datetime.now(UTC) - timedelta(hours=2)
        _store(db, snapshot)
        [doc] = db.catalog.list_documents(collection_filter="r-captures")
        stored = datetime.fromisoformat(doc["ingestion_timestamp"])
        assert abs(stored - snapshot) < timedelta(seconds=1)

    def test_it_is_the_queue_time_when_none_is_given(self, db: Database) -> None:
        before = datetime.now(UTC)
        _store(db, None)
        [doc] = db.catalog.list_documents(collection_filter="r-captures")
        assert datetime.fromisoformat(doc["ingestion_timestamp"]) >= before


class TestTheRace:
    """The transcript is written after the snapshot read but before the job runs."""

    @staticmethod
    def _transcript(tmp_path: Path, mtime: datetime) -> Path:
        path = tmp_path / "abcd1234-0000-0000-0000-000000000000.jsonl"
        path.write_text("{}\n")
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))
        return path

    def test_a_write_after_the_snapshot_is_seen_as_grown(
        self, db: Database, tmp_path: Path
    ) -> None:
        snapshot = datetime.now(UTC) - timedelta(minutes=5)
        # The job runs now, long after; the transcript grew 1 minute after the read.
        transcript = self._transcript(tmp_path, snapshot + timedelta(minutes=1))
        _store(db, snapshot)
        stored = StoredSessions(db.catalog.list_documents("r-captures"))
        assert not stored.is_current(transcript)

    def test_without_a_snapshot_the_same_write_hides_behind_the_queue_time(
        self, db: Database, tmp_path: Path
    ) -> None:
        snapshot = datetime.now(UTC) - timedelta(minutes=5)
        transcript = self._transcript(tmp_path, snapshot + timedelta(minutes=1))
        _store(db, None)
        stored = StoredSessions(db.catalog.list_documents("r-captures"))
        assert stored.is_current(transcript)  # the race this change closes

    def test_a_transcript_untouched_since_the_snapshot_is_current(
        self, db: Database, tmp_path: Path
    ) -> None:
        snapshot = datetime.now(UTC) - timedelta(minutes=5)
        transcript = self._transcript(tmp_path, snapshot - timedelta(minutes=1))
        _store(db, snapshot)
        stored = StoredSessions(db.catalog.list_documents("r-captures"))
        assert stored.is_current(transcript)
