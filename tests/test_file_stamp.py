"""FileStamp: what a file must still be for a cached verdict to hold."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.file_stamp import FileStamp


def test_a_stamp_round_trips_through_json() -> None:
    stamp = FileStamp(1_700_000_000_123_456_789, 42)

    assert FileStamp.from_json(stamp.as_json()) == stamp


@pytest.mark.parametrize(
    "raw",
    [
        None,
        5,
        "x",
        [],
        [1],
        [1, 2],
        [1, 2, 3],
        [1, 2, 3, "4"],
        [1.5, 2, 3, 4],
        [True, 2, 3, 4],
        {"a": 1},
    ],
)
def test_anything_that_is_not_a_pair_of_ints_is_not_a_stamp(raw: object) -> None:
    with pytest.raises(ValueError, match="not a file stamp"):
        FileStamp.from_json(raw)


def test_the_stamp_names_the_inode_and_device(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("{}\n")

    stamp = FileStamp.of(path.stat())

    assert (stamp.ino, stamp.dev) == (path.stat().st_ino, path.stat().st_dev)
