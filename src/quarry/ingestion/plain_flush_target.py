"""The flush target for single-document ingest: write chunks, keep no registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.db.chunk_table import ChunkTable

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np
    from numpy.typing import NDArray

    from quarry.db.chunk_store import ChunkStore
    from quarry.ingestion.progressive import FlushCheckpoint
    from quarry.models import Chunk


@final
class PlainFlushTarget:
    """FlushTarget for single-document ingest: write chunks, no registry."""

    __slots__ = ("_store",)

    _store: ChunkStore

    def __new__(cls, store: ChunkStore) -> Self:
        self = super().__new__(cls)
        self._store = store
        return self

    def build_records(
        self, chunks: list[Chunk], vectors: NDArray[np.float32]
    ) -> list[dict[str, object]]:
        return ChunkTable.build_records(chunks, vectors)

    def insert_records(self, records: list[dict[str, object]]) -> int:
        return self._store.insert_records(records)

    def on_flush(self, checkpoints: Sequence[FlushCheckpoint]) -> None:
        """Single-document ingest keeps no resume watermark — nothing to commit."""
