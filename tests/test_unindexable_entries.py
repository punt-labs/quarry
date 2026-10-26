"""StampedEntries: which entries of a saved cache can be trusted, and what is pruned."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from quarry.file_stamp import FileStamp
from quarry.unindexable_entries import StampedEntries

_SOURCE = Path("/state/backfill-unindexable.json")


def test_a_clean_object_is_kept_whole_and_is_not_dirty() -> None:
    entries = StampedEntries({"/a": [1, 2, 3, 4], "/b/c": [5, 6, 7, 8]}, _SOURCE)

    assert entries.kept == {"/a": FileStamp(1, 2, 3, 4), "/b/c": FileStamp(5, 6, 7, 8)}
    assert not entries.dirty


@pytest.mark.parametrize(
    "bad_key", ["relative", "", "a/b", "/with\0nul", 5, None, ("/a",)]
)
def test_a_key_that_cannot_name_a_transcript_is_dropped(bad_key: object) -> None:
    raw: dict[object, object] = {bad_key: [1, 2, 3, 4], "/ok": [1, 2, 3, 4]}

    entries = StampedEntries(raw, _SOURCE)

    assert list(entries.kept) == ["/ok"]
    assert entries.dirty


def test_something_that_is_not_an_object_is_empty_and_dirty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="quarry.unindexable_entries"):
        entries = StampedEntries([1, 2, 3, 4], _SOURCE)

    assert entries.kept == {}
    assert entries.dirty
    assert "not an object (list)" in caplog.text
    assert str(_SOURCE) in caplog.text


def test_what_was_dropped_is_counted_in_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="quarry.unindexable_entries"):
        StampedEntries({"/a": "x", "b": [1, 2, 3, 4], "/c": [1, 2, 3, 4]}, _SOURCE)

    assert caplog.text.count("dropped 2 entries") == 1


def test_prune_keeps_only_what_can_still_be_examined(tmp_path: Path) -> None:
    kept = tmp_path / "kept.jsonl"
    kept.write_text("{}\n")
    stamp = FileStamp(1, 1)

    live = StampedEntries.prune(
        {str(kept): stamp, str(tmp_path / "gone"): stamp, "/tmp/with\0nul": stamp}
    )

    assert live == {str(kept): stamp}
