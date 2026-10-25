"""The newest message records of a JSONL transcript, parsed one line at a time."""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, final

from quarry.transcript_turns import TurnText

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)


@final
class TranscriptRecords:
    """Parse a transcript into its message records, keeping the newest :attr:`MAX`.

    Only a record that carries message text is kept, and only those count against
    the bound: a record with no text (a file snapshot, a tool event, an empty
    object) can never change what the conversation says, so a flood of them can
    never push the messages out.  A line that is not a JSON object (or nests too
    deeply to parse) is skipped.  Lines are parsed one at a time and the oldest
    records are dropped as the list grows, so the memory held is the records kept,
    not a list of every line; :meth:`parse` says how many it dropped.  Measured
    with ``tracemalloc`` on pathological 32 MiB inputs, beyond the text itself:
    11 million ``{}`` lines peaked at 1.3 GiB before the bound and 135 MiB with
    it (they are no longer kept at all); 559 thousand one-line messages, 431 MiB
    before and 201 MiB with it (the worst case now); 167 non-ASCII lines of
    100 kB, 79 MiB.
    """

    MAX: ClassVar[int] = 100_000
    TRIM: ClassVar[int] = 10_000

    __slots__ = ()

    @classmethod
    def parse(cls, raw: str) -> ParsedRecords:
        """Return the newest message records of *raw* and how many were dropped."""
        records: list[dict[str, object]] = []
        dropped = 0
        for record in cls._messages(raw):
            records.append(record)
            if len(records) >= cls.MAX + cls.TRIM:
                del records[: cls.TRIM]  # amortised: the oldest are dropped
                dropped += cls.TRIM
        excess = max(len(records) - cls.MAX, 0)
        return ParsedRecords(tuple(records[excess:]), dropped + excess)

    @staticmethod
    def _messages(raw: str) -> Iterator[dict[str, object]]:
        """Yield each record of *raw* that carries message text, in file order."""
        for line in io.StringIO(raw):
            try:
                obj = json.loads(line)
            except (ValueError, TypeError, RecursionError):
                continue
            if isinstance(obj, dict) and TurnText.turn_text(obj):
                yield obj


@final
@dataclass(frozen=True, slots=True)
class ParsedRecords:
    """The records a transcript kept, and how many of its oldest the bound dropped."""

    records: tuple[dict[str, object], ...]
    dropped: int

    def warn_if_truncated(self, path: Path) -> None:
        """Warn once, naming the transcript, if the record bound dropped any."""
        if self.dropped:
            logger.warning(
                "transcript: %s: dropped its %d oldest records past the record bound",
                path.name,
                self.dropped,
            )
