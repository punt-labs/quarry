"""A transcript as it was when backfill read it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


@final
@dataclass(frozen=True, slots=True)
class TranscriptRead:
    """A transcript with its mtime and the moment the read began.

    The mtime is taken before the read and the snapshot before that, so a write
    after either is newer than what is stored or recorded, whatever happens
    between the read and the store.
    """

    transcript: Path
    mtime_ns: int
    snapshot: datetime
