"""StampCheck: whether a stamp taken at a read can still be trusted."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quarry.file_stamp import FileStamp
from quarry.stamp_check import StampCheck

_READ_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _mtime_ns(seconds_before_read: float) -> int:
    return int((_READ_AT - timedelta(seconds=seconds_before_read)).timestamp() * 1e9)


@pytest.mark.parametrize(
    ("seconds_before_read", "racy"),
    [(0.0, True), (1.0, True), (1.999, True), (2.001, False), (60.0, False)],
)
def test_a_file_modified_within_two_seconds_of_the_read_is_racy(
    seconds_before_read: float, *, racy: bool
) -> None:
    stamp = FileStamp(_mtime_ns(seconds_before_read), 10)

    assert StampCheck(stamp).is_racy(_READ_AT) is racy


def test_a_file_modified_after_the_read_is_racy() -> None:
    assert StampCheck(FileStamp(_mtime_ns(-5.0), 10)).is_racy(_READ_AT)


def test_a_stamp_holds_for_the_file_it_was_taken_from(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("{}\n")

    assert StampCheck(FileStamp.of(path.stat())).holds_for(path)


def test_a_stamp_stops_holding_when_the_mtime_or_the_size_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("{}\n")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    stamp = FileStamp.of(path.stat())

    os.utime(path, (1_700_000_060, 1_700_000_060))
    assert not StampCheck(stamp).holds_for(path)

    os.utime(path, (1_700_000_000, 1_700_000_000))
    assert StampCheck(stamp).holds_for(path)
    path.write_text("{}\n{}\n")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    assert not StampCheck(stamp).holds_for(path)


def test_a_stamp_does_not_hold_for_a_file_that_is_gone_or_not_a_path(
    tmp_path: Path,
) -> None:
    stamp = FileStamp(1, 1)

    assert not StampCheck(stamp).holds_for(tmp_path / "gone")
    assert not StampCheck(stamp).holds_for(Path("/tmp/with\0nul"))


def test_a_file_moved_over_the_path_does_not_hold_even_with_its_size_and_mtime(
    tmp_path: Path,
) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("{}\n")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    stamp = FileStamp.of(path.stat())
    other = tmp_path / "other.jsonl"
    other.write_text("{}\n")  # the same size, and made to look the same age
    os.utime(other, (1_700_000_000, 1_700_000_000))
    other.replace(path)  # another file moved over it

    assert not StampCheck(stamp).holds_for(path)
