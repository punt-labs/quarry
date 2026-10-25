"""TranscriptRecords: the newest message records of a transcript, bounded in memory."""

from __future__ import annotations

import json
import tracemalloc
from unittest.mock import patch

from quarry.transcript_records import TranscriptRecords
from quarry.transcript_turns import TurnText


def _message(n: int) -> str:
    record = {"type": "user", "message": {"role": "user", "content": f"m{n}"}}
    return json.dumps(record)


def _texts(raw: str) -> list[str]:
    parsed = TranscriptRecords.parse(raw)
    return [TurnText.turn_text(record) for record in parsed.records]


def test_every_message_is_kept_in_file_order_and_nothing_is_dropped() -> None:
    raw = "\n".join(_message(n) for n in range(3))

    assert TranscriptRecords.parse(raw).dropped == 0
    assert _texts(raw) == ["m0", "m1", "m2"]


def test_lines_that_are_not_message_objects_are_skipped() -> None:
    raw = "\n".join([_message(1), "not json", "[1, 2]", "{}", "", _message(2)])

    assert _texts(raw) == ["m1", "m2"]


def test_a_line_nested_too_deeply_to_parse_is_skipped() -> None:
    raw = "[" * 200_000 + "\n" + _message(1)

    assert _texts(raw) == ["m1"]


def test_only_the_newest_messages_are_kept_and_the_drop_is_counted() -> None:
    raw = "\n".join(_message(n) for n in range(25))

    with (
        patch.object(TranscriptRecords, "MAX", 10),
        patch.object(TranscriptRecords, "TRIM", 3),
    ):
        parsed = TranscriptRecords.parse(raw)

    kept = [TurnText.turn_text(r) for r in parsed.records]
    assert kept == [f"m{n}" for n in range(15, 25)]
    assert parsed.dropped == 15


def test_old_messages_survive_a_flood_of_records_with_no_text() -> None:
    flood = "{}\n" * (TranscriptRecords.MAX + TranscriptRecords.TRIM + 5)
    raw = _message(1) + "\n" + flood + _message(2)

    parsed = TranscriptRecords.parse(raw)

    assert parsed.dropped == 0  # records with no text never count against the bound
    assert [TurnText.turn_text(r) for r in parsed.records] == ["m1", "m2"]


def _peak_bytes(raw: str, *, bound: int, trim: int) -> int:
    """Return the tracemalloc peak of parsing *raw* with the given bound."""
    with (
        patch.object(TranscriptRecords, "MAX", bound),
        patch.object(TranscriptRecords, "TRIM", trim),
    ):
        tracemalloc.start()
        try:
            TranscriptRecords.parse(raw)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()


def test_memory_held_follows_the_bound_not_the_number_of_messages() -> None:
    raw = "\n".join(_message(n) for n in range(30_000))

    bounded = _peak_bytes(raw, bound=300, trim=30)
    unbounded = _peak_bytes(raw, bound=10**9, trim=10**9)

    assert bounded * 2 < unbounded
