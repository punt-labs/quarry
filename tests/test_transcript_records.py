"""TranscriptRecords: the newest records of a transcript, bounded in memory."""

from __future__ import annotations

import json
from unittest.mock import patch

from quarry.transcript_records import TranscriptRecords


def _line(n: int) -> str:
    return json.dumps({"n": n})


def test_every_object_line_is_kept_in_file_order() -> None:
    raw = "\n".join(_line(n) for n in range(3))

    assert TranscriptRecords.parse(raw) == ({"n": 0}, {"n": 1}, {"n": 2})


def test_lines_that_are_not_objects_are_skipped() -> None:
    raw = "\n".join([_line(1), "not json", "[1, 2]", "", _line(2)])

    assert TranscriptRecords.parse(raw) == ({"n": 1}, {"n": 2})


def test_a_line_nested_too_deeply_to_parse_is_skipped() -> None:
    raw = "[" * 200_000 + "\n" + _line(1)

    assert TranscriptRecords.parse(raw) == ({"n": 1},)


def test_only_the_newest_records_are_kept() -> None:
    raw = "\n".join(_line(n) for n in range(25))

    with (
        patch.object(TranscriptRecords, "MAX", 10),
        patch.object(TranscriptRecords, "TRIM", 3),
    ):
        kept = TranscriptRecords.parse(raw)

    assert [record["n"] for record in kept] == list(range(15, 25))


def test_the_default_bound_holds_a_file_of_empty_objects() -> None:
    kept = TranscriptRecords.parse("{}\n" * (TranscriptRecords.MAX + 5))

    assert len(kept) == TranscriptRecords.MAX
