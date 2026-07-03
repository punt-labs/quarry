"""Streaming document producer: chunk once, embed in bounded windows (DES-034)."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Self

from quarry.db.chunk_table import ChunkTable
from quarry.ingestion.backends import get_embedding_backend
from quarry.ingestion.chunker import chunk_pages
from quarry.ingestion.progressive import ProgressiveIndexer

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    import numpy as np
    from numpy.typing import NDArray

    from quarry.config import Settings
    from quarry.db.chunk_store import ChunkStore
    from quarry.ingestion.progressive import FlushCheckpoint
    from quarry.models import Chunk, PageContent


class DocumentStreamer:
    """Chunk a document once, then embed it in bounded windows.

    Owns the sole assignment of a document-global, contiguous ``chunk_index`` so
    within-file resume can delete and re-embed the tail unambiguously (DES-034
    §5.3). Embedding is deferred to ``stream_batches`` so a large document never
    materializes all its vectors at once.
    """

    __slots__ = ("_settings",)

    _settings: Settings

    def __new__(cls, settings: Settings) -> Self:
        self = super().__new__(cls)
        self._settings = settings
        return self

    def build_chunks(
        self,
        pages: list[PageContent],
        *,
        collection: str = "default",
        source_format: str = "",
        agent_handle: str = "",
        memory_type: str = "",
        summary: str = "",
    ) -> list[Chunk]:
        """Chunk *pages* and assign a document-global, contiguous ``chunk_index``."""
        chunks = chunk_pages(
            pages,
            max_chars=self._settings.chunk_max_chars,
            overlap_chars=self._settings.chunk_overlap_chars,
            collection=collection,
            source_format=source_format,
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
        )
        return [replace(chunk, chunk_index=i) for i, chunk in enumerate(chunks)]

    def stream_batches(
        self,
        chunks: list[Chunk],
        *,
        start_index: int = 0,
    ) -> Iterator[tuple[list[Chunk], NDArray[np.float32]]]:
        """Embed ``chunks[start_index:]`` in bounded windows, yielding each.

        Peak resident vectors are one window (``embed_window_chunks``). For
        ``start_index > 0`` the prefix is not embedded — the caller ran
        delete-tail (DES-034 §5.3, G1).
        """
        window = max(1, self._settings.embed_window_chunks)
        embedder = get_embedding_backend(self._settings)
        tail = chunks[start_index:]
        for offset in range(0, len(tail), window):
            batch = tail[offset : offset + window]
            vectors: NDArray[np.float32] = embedder.embed_texts([c.text for c in batch])
            yield batch, vectors


class _PlainFlushTarget:
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


def progressive_insert(
    chunks: list[Chunk],
    store: ChunkStore,
    settings: Settings,
    document_name: str,
) -> int:
    """Embed *chunks* in windows and progressively insert via ProgressiveIndexer.

    Bounds peak resident vectors to ``sync_flush_mb + one window`` and makes each
    flushed window searchable, even for a single very large document.
    """
    target = _PlainFlushTarget(store)
    indexer = ProgressiveIndexer(
        target, flush_bytes=settings.sync_flush_mb * 1024 * 1024
    )
    indexer.begin_file(document_name, total_chunks=len(chunks))
    for batch, vectors in DocumentStreamer(settings).stream_batches(chunks):
        indexer.add_window(document_name, batch, vectors)
    indexer.complete_file(document_name)
    indexer.drain()
    return indexer.inserted_count
