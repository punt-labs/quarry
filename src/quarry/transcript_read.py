"""A transcript as it was when backfill read it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from quarry.file_stamp import FileStamp


@final
@dataclass(frozen=True, slots=True)
class TranscriptRead:
    """A transcript with its stamp and the moment the read began.

    The snapshot is taken before the read, so a write after it is newer than
    what is stored or recorded, whatever happens between the read and the store.
    """

    transcript: Path
    stamp: FileStamp
    snapshot: datetime
