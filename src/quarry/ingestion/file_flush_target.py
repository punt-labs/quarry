"""The flush sink for one file's ``index_one`` run.

Separated from :class:`~quarry.ingestion.file_indexer.SingleFileIndexer` because
the two have different lifetimes.  The indexer is reused across files; a flush
sink belongs to exactly one file's run and closes over that file's ``FileMeta``.
Holding the meta on the long-lived indexer instead meant setting an attribute
before the run and clearing it in a ``finally`` — transient mutable state whose
correctness rested on nothing else touching the indexer meanwhile.  Constructing
one sink per run makes that impossible to get wrong rather than merely unlikely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.db.chunk_table import ChunkTable

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np
    from numpy.typing import NDArray

    from quarry.db.chunk_store import ChunkStore
    from quarry.ingestion.file_indexer import SingleFileIndexer
    from quarry.ingestion.progressive import FlushCheckpoint
    from quarry.models import Chunk
    from quarry.sync_messages import FileMeta
    from quarry.sync_registry import SyncRegistry


@final
class SingleFileFlushTarget:
    """Write one file's flushed windows to LanceDB and checkpoint its row.

    Satisfies :class:`~quarry.ingestion.progressive.FlushTarget` structurally.
    """

    __slots__ = ("_indexer", "_meta", "_registry", "_store")

    _store: ChunkStore
    _registry: SyncRegistry
    _indexer: SingleFileIndexer
    _meta: FileMeta

    def __new__(
        cls,
        indexer: SingleFileIndexer,
        store: ChunkStore,
        registry: SyncRegistry,
        meta: FileMeta,
    ) -> Self:
        """Bind flushed rows and resume checkpoints to one file's metadata."""
        self = super().__new__(cls)
        self._indexer = indexer
        self._store = store
        self._registry = registry
        self._meta = meta
        return self

    def build_records(
        self, chunks: list[Chunk], vectors: NDArray[np.float32]
    ) -> list[dict[str, object]]:
        """Build LanceDB row dicts for one embed window."""
        return ChunkTable.build_records(chunks, vectors)

    def insert_records(self, records: list[dict[str, object]]) -> int:
        """Append one flush's rows to LanceDB."""
        return self._store.insert_records(records)

    def on_flush(self, checkpoints: Sequence[FlushCheckpoint]) -> None:
        """Commit this file's watermark row(s) in one registry transaction."""
        for checkpoint in checkpoints:
            self._registry.files.upsert_file(
                self._indexer.checkpoint_row(self._meta, checkpoint), commit=False
            )
        self._registry.commit()
