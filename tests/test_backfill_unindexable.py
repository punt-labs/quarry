"""UnindexableTranscripts: what backfill learned to skip survives the run."""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from quarry.atomic_file import AtomicFile
from quarry.backfill_unindexable import UnindexableTranscripts
from quarry.transcript_read import TranscriptRead

if TYPE_CHECKING:
    from pathlib import Path


_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _read(transcript: Path) -> TranscriptRead:
    return TranscriptRead(transcript, transcript.stat().st_mtime_ns, _NOW)


def _transcript(tmp_path: Path) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("{}\n")
    return path


def test_a_recorded_transcript_is_known_until_it_changes(tmp_path: Path) -> None:
    store = UnindexableTranscripts(tmp_path / "state.json")
    transcript = _transcript(tmp_path)
    assert not store.knows(transcript)

    store.record(_read(transcript))
    assert store.knows(transcript)

    later = transcript.stat().st_mtime + 60
    os.utime(transcript, (later, later))
    assert not store.knows(transcript)


def test_what_was_learned_is_known_to_the_next_run(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    transcript = _transcript(tmp_path)
    first = UnindexableTranscripts(state)
    first.record(_read(transcript))
    first.save()

    assert UnindexableTranscripts(state).knows(transcript)


def test_nothing_is_written_when_nothing_was_learned(tmp_path: Path) -> None:
    state = tmp_path / "state.json"

    UnindexableTranscripts(state).save()

    assert not state.exists()


def test_an_unreadable_file_starts_empty_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = tmp_path / "state.json"
    state.write_text("{not json")
    transcript = _transcript(tmp_path)

    with caplog.at_level(logging.WARNING, logger="quarry.backfill_unindexable_file"):
        store = UnindexableTranscripts(state)

    assert not store.knows(transcript)
    assert "unreadable" in caplog.text


def test_a_vanished_transcript_is_not_known(tmp_path: Path) -> None:
    store = UnindexableTranscripts(tmp_path / "state.json")

    assert not store.knows(tmp_path / "gone.jsonl")


def _state_of(store: Path) -> dict[str, int]:
    return cast("dict[str, int]", json.loads(store.read_text()))


def test_the_file_is_written_with_mode_0600(tmp_path: Path) -> None:
    store = tmp_path / "state.json"
    transcript = _transcript(tmp_path)
    cache = UnindexableTranscripts(store)
    cache.record(_read(transcript))

    assert cache.save() == ""

    assert stat.S_IMODE(store.stat().st_mode) == 0o600


def test_a_transcript_that_changed_since_the_read_is_not_recorded(
    tmp_path: Path,
) -> None:
    store = tmp_path / "state.json"
    transcript = _transcript(tmp_path)
    read_at = transcript.stat().st_mtime_ns
    later = transcript.stat().st_mtime + 60
    os.utime(transcript, (later, later))  # appended to after the read
    cache = UnindexableTranscripts(store)

    cache.record(TranscriptRead(transcript, read_at, _NOW))

    assert not cache.knows(transcript)
    assert cache.save() == ""
    assert not store.exists()


def test_a_failed_record_is_logged_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache = UnindexableTranscripts(tmp_path / "state.json")

    with caplog.at_level(logging.DEBUG, logger="quarry.backfill_unindexable"):
        cache.record(TranscriptRead(tmp_path / "gone.jsonl", 1, _NOW))

    assert "ENOENT" in caplog.text


def test_a_failed_save_is_reported_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = tmp_path / "state.json"
    transcript = _transcript(tmp_path)
    cache = UnindexableTranscripts(store)
    cache.record(_read(transcript))

    with (
        patch.object(
            AtomicFile, "replace", side_effect=OSError(errno.ENOSPC, "no space")
        ),
        caplog.at_level(logging.WARNING, logger="quarry.backfill_unindexable_file"),
    ):
        fault = cache.save()

    assert fault == "ENOSPC"
    assert str(store) in caplog.text
    assert "ENOSPC" in caplog.text


def test_entries_for_transcripts_that_are_gone_are_pruned_at_save(
    tmp_path: Path,
) -> None:
    store = tmp_path / "state.json"
    kept = _transcript(tmp_path)
    gone = tmp_path / "gone.jsonl"
    gone.write_text("{}\n")
    cache = UnindexableTranscripts(store)
    cache.record(_read(kept))
    cache.record(_read(gone))
    gone.unlink()

    cache.save()

    assert set(_state_of(store)) == {str(kept)}


@pytest.mark.parametrize(
    ("raw", "expected_in_log"),
    [
        ("[1, 2]", "not an object (list)"),
        ('{"a": "x", "b": true, "c": 1.5}', "dropped 3 entries"),
    ],
)
def test_a_file_with_the_wrong_shape_says_so_and_keeps_only_mtimes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    raw: str,
    expected_in_log: str,
) -> None:
    store = tmp_path / "state.json"
    store.write_text(raw)

    with caplog.at_level(logging.WARNING, logger="quarry.backfill_unindexable_file"):
        cache = UnindexableTranscripts(store)

    assert expected_in_log in caplog.text
    assert str(store) in caplog.text
    assert not cache.knows(tmp_path / "a")


def test_an_oversized_file_is_ignored_unread(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "state.json"
    store.write_text("{}" + " " * 100)
    monkeypatch.setattr("quarry.backfill_unindexable_file._MAX_BYTES", 10)

    with caplog.at_level(logging.WARNING, logger="quarry.backfill_unindexable_file"):
        UnindexableTranscripts(store)

    assert "over 10 bytes" in caplog.text
    assert str(store) in caplog.text


def test_a_file_that_recurses_too_deeply_starts_empty_and_names_the_class(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = tmp_path / "state.json"
    store.write_text("[" * 100_000)

    with caplog.at_level(logging.WARNING, logger="quarry.backfill_unindexable_file"):
        UnindexableTranscripts(store)

    assert "RecursionError" in caplog.text
    assert str(store) in caplog.text
