"""The newest JSON objects of a JSONL transcript, parsed one line at a time."""

from __future__ import annotations

import io
import json
from typing import ClassVar, final


@final
class TranscriptRecords:
    """Parse a transcript's text into records, keeping the newest :attr:`MAX`.

    A line that is not a JSON object (or nests too deeply to parse) is skipped,
    so no extraction ever sees a partial or malformed record.  Lines are parsed
    one at a time and the oldest records are dropped as the list grows, so the
    memory held is the records kept, not a list of every line.  Measured with
    ``tracemalloc`` on pathological 32 MiB inputs, beyond the text itself: 11
    million ``{}`` lines peaked at 1.3 GiB before the bound and 135 MiB with it;
    559 thousand one-line messages, 431 MiB before and 201 MiB with it (the worst
    case now); 167 non-ASCII lines of 100 kB, 79 MiB.
    """

    MAX: ClassVar[int] = 100_000
    TRIM: ClassVar[int] = 10_000

    __slots__ = ()

    @classmethod
    def parse(cls, raw: str) -> tuple[dict[str, object], ...]:
        """Return the newest parsed JSON objects in *raw*, in file order."""
        records: list[dict[str, object]] = []
        for line in io.StringIO(raw):
            try:
                obj = json.loads(line)
            except (ValueError, TypeError, RecursionError):
                continue
            if isinstance(obj, dict):
                records.append(obj)
            if len(records) >= cls.MAX + cls.TRIM:
                del records[: cls.TRIM]  # amortised: the oldest are dropped
        return tuple(records[-cls.MAX :])
