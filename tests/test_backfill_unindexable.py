"""UnindexableTranscripts: what backfill learned to skip survives the run."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from quarry.backfill_unindexable import UnindexableTranscripts

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _transcript(tmp_path: Path) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("{}\n")
    return path


def test_a_recorded_transcript_is_known_until_it_changes(tmp_path: Path) -> None:
    store = UnindexableTranscripts(tmp_path / "state.json")
    transcript = _transcript(tmp_path)
    assert not store.knows(transcript)

    store.record(transcript)
    assert store.knows(transcript)

    later = transcript.stat().st_mtime + 60
    os.utime(transcript, (later, later))
    assert not store.knows(transcript)


def test_what_was_learned_is_known_to_the_next_run(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    transcript = _transcript(tmp_path)
    first = UnindexableTranscripts(state)
    first.record(transcript)
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

    with caplog.at_level(logging.WARNING, logger="quarry.backfill_unindexable"):
        store = UnindexableTranscripts(state)

    assert not store.knows(transcript)
    assert "unreadable" in caplog.text


def test_a_vanished_transcript_is_not_known(tmp_path: Path) -> None:
    store = UnindexableTranscripts(tmp_path / "state.json")

    assert not store.knows(tmp_path / "gone.jsonl")
