"""UnindexableTranscripts: what backfill learned to skip survives the run."""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from quarry.atomic_file import AtomicFile
from quarry.backfill_unindexable import UnindexableTranscripts
from quarry.file_stamp import FileStamp
from quarry.transcript_read import TranscriptRead
from quarry.unindexable_entries import StampedEntries

if TYPE_CHECKING:
    from collections.abc import Callable

_OLD = 1_700_000_000  # an mtime well outside the racy window of any read below


def _read(transcript: Path, *, snapshot: datetime | None = None) -> TranscriptRead:
    return TranscriptRead(
        transcript,
        FileStamp.of(transcript.stat()),
        snapshot or datetime.now(UTC),
    )


def _transcript(tmp_path: Path, *, mtime: float = _OLD) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("{}\n")
    os.utime(path, (mtime, mtime))
    return path


def test_a_recorded_transcript_is_known_until_it_changes(tmp_path: Path) -> None:
    store = UnindexableTranscripts(tmp_path / "state.json")
    transcript = _transcript(tmp_path)
    assert not store.knows(transcript)

    store.record(_read(transcript))
    assert store.knows(transcript)

    os.utime(transcript, (_OLD + 60, _OLD + 60))
    assert not store.knows(transcript)


def test_a_transcript_rewritten_to_the_same_mtime_but_another_size_is_not_known(
    tmp_path: Path,
) -> None:
    store = UnindexableTranscripts(tmp_path / "state.json")
    transcript = _transcript(tmp_path)
    store.record(_read(transcript))

    transcript.write_text("{}\n{}\n")
    os.utime(transcript, (_OLD, _OLD))  # the same mtime, a different size

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


def _state_of(store: Path) -> dict[str, list[int]]:
    return cast("dict[str, list[int]]", json.loads(store.read_text()))


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
    read = _read(transcript)
    os.utime(transcript, (_OLD + 60, _OLD + 60))  # appended to after the read
    cache = UnindexableTranscripts(store)

    cache.record(read)

    assert not cache.knows(transcript)
    assert cache.save() == ""
    assert not store.exists()


def test_a_transcript_modified_within_two_seconds_of_the_read_is_not_recorded(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    transcript = _transcript(tmp_path, mtime=(now - timedelta(seconds=1)).timestamp())
    cache = UnindexableTranscripts(tmp_path / "state.json")

    cache.record(_read(transcript, snapshot=now))  # a write in this tick could follow

    assert not cache.knows(transcript)


def test_a_transcript_last_modified_just_outside_the_window_is_recorded(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    transcript = _transcript(tmp_path, mtime=(now - timedelta(seconds=3)).timestamp())
    cache = UnindexableTranscripts(tmp_path / "state.json")

    cache.record(_read(transcript, snapshot=now))

    assert cache.knows(transcript)


def test_a_failed_record_is_logged_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache = UnindexableTranscripts(tmp_path / "state.json")
    gone = tmp_path / "gone.jsonl"

    with caplog.at_level(logging.DEBUG, logger="quarry.stamp_check"):
        cache.record(TranscriptRead(gone, FileStamp(_OLD, 1), datetime.now(UTC)))

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
    os.utime(gone, (_OLD, _OLD))
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
        ('{"/a": "x", "/b": true, "/c": 1.5, "/d": [1], "/e": [1, true]}', "dropped 5"),
        ('{"relative/path": [1, 2]}', "dropped 1"),
        ('{"/with\\u0000nul": [1, 2]}', "dropped 1"),
    ],
)
def test_a_file_with_the_wrong_shape_says_so_and_keeps_only_stamps_of_paths(
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
    assert not cache.knows(Path("/a"))


def test_a_file_that_lost_entries_is_rewritten_clean_and_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = tmp_path / "state.json"
    transcript = _transcript(tmp_path)
    good = {str(transcript): FileStamp.of(transcript.stat()).as_json()}
    store.write_text(json.dumps({**good, "relative": [1, 2], "/x": 5}))

    with caplog.at_level(logging.WARNING, logger="quarry.backfill_unindexable_file"):
        first = UnindexableTranscripts(store)
        assert first.save() == ""  # nothing new, but it was dirty: rewritten
        UnindexableTranscripts(store)

    assert _state_of(store) == good
    assert caplog.text.count("dropped 2") == 1  # the second load found it clean


@pytest.mark.parametrize(
    "damage",
    [
        lambda store: store.write_text("{not json"),
        lambda store: store.write_text("[1, 2]"),
    ],
    ids=["unreadable", "not-an-object"],
)
def test_a_file_that_could_not_be_used_is_rewritten_by_the_next_save(
    tmp_path: Path, damage: Callable[[Path], object]
) -> None:
    store = tmp_path / "state.json"
    damage(store)

    assert UnindexableTranscripts(store).save() == ""

    assert _state_of(store) == {}


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


def test_a_path_with_a_nul_never_makes_the_cache_raise(tmp_path: Path) -> None:
    store = tmp_path / "state.json"
    transcript = _transcript(tmp_path)
    nul = Path("/tmp/with\0nul")  # not constructible on disk, but a Path can say it
    cache = UnindexableTranscripts(store)
    cache.record(_read(transcript))

    cache.record(TranscriptRead(nul, FileStamp(_OLD, 1), datetime.now(UTC)))
    assert not cache.knows(nul)
    assert cache.save() == ""  # and the save does not raise on it either

    assert set(_state_of(store)) == {str(transcript)}


def test_a_key_with_a_nul_is_pruned_not_raised_on() -> None:
    stamp = FileStamp(_OLD, 1)

    assert StampedEntries.prune({"/tmp/with\0nul": stamp}) == {}
