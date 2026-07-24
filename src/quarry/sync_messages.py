"""Queue messages for the progressive sync producer/consumer protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from quarry.models import Chunk
    from quarry.sync_file_store import FileRecord


@dataclass(frozen=True, slots=True)
class FileMeta:
    """Per-file context the consumer needs to begin and checkpoint a file."""

    record: FileRecord
    resume_watermark: int
    total_chunks: int


@dataclass(frozen=True, slots=True)
class WindowMsg:
    """One queue item: an embed window, or a file-complete/error sentinel."""

    file_id: str
    batch: list[Chunk]
    # None on the final sentinel — a completion/error carries no vectors.
    vectors: NDArray[np.float32] | None
    final: bool = False
    # Set only when a producer failed before completing the file.
    error: str | None = None
    # On a failed sentinel: drop the stored (now stale) document on the consumer.
    clear_stale: bool = False
