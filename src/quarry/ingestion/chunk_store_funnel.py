"""The single convergence point for chunking, embedding, and storing pages.

Every ingest path — file, inline content, and URL — funnels through
``_chunk_embed_store`` (DES-036), so the overwrite-delete gate lives in exactly
one place and is mechanically checkable: ``grep -rn "_chunk_embed_store(" src/``
shows one ``def``.  The funnel is modeled as :class:`ChunkStoreFunnel` — a
Facade (PY-DP-10) over its three phases (chunk, embed-and-store, or the
fail-closed skip) — rather than one long function, so each phase is
independently named and testable.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, cast, final

from quarry.capture_url import CaptureUrl
from quarry.ingestion.streaming import DocumentStreamer, progressive_insert

if TYPE_CHECKING:
    from quarry.ingestion.extracted_document import ExtractedDocument
    from quarry.ingestion.ingest_context import IngestContext, Progress
    from quarry.ingestion.ingest_stats import IngestStats
    from quarry.models import Chunk
    from quarry.results import IngestResult

logger = logging.getLogger(__name__)


@final
class ChunkStoreFunnel:
    """DES-036's single convergence point: chunk, embed, and store pages."""

    @staticmethod
    def _chunk_embed_store(
        extracted: ExtractedDocument,
        document_name: str,
        progress: Progress,
        context: IngestContext,
    ) -> IngestResult:
        """Shared pipeline: chunk pages, embed in bounded windows, store progressively.

        The overwrite-delete lives HERE — the one point every ingest path converges
        on — and is gated on there being CHUNKS to store, not merely non-empty pages.
        Pages that parse but chunk to nothing (unchunkable/empty sections) must not
        delete a prior good document and then store nothing: the fail-closed delete
        fires only when a replacement chunk set actually exists.
        """
        progress("Chunking")
        t0 = time.perf_counter()
        chunks = DocumentStreamer(context.settings).build_chunks(
            extracted.pages,
            collection=context.collection,
            source_format=extracted.source_format,
            agent_handle=context.agent_handle,
            memory_type=context.memory_type,
            summary=context.summary,
        )
        logger.info(
            "pipeline: chunked %d pages → %d chunks in %.2fs",
            len(extracted.pages),
            len(chunks),
            time.perf_counter() - t0,
        )
        progress("Created %d chunks", len(chunks))

        if chunks:
            inserted = ChunkStoreFunnel._embed_and_store(
                chunks, document_name, progress, context
            )
        else:
            inserted = ChunkStoreFunnel._skip(document_name, progress)

        return ChunkStoreFunnel._build_result(
            document_name, inserted, extracted.stats, context.collection
        )

    @staticmethod
    def _embed_and_store(
        chunks: list[Chunk],
        document_name: str,
        progress: Progress,
        context: IngestContext,
    ) -> int:
        """Delete the prior document (if overwriting), then embed and store."""
        if context.overwrite:
            context.database.store.delete_document(
                document_name, collection=context.collection, count=False
            )
        progress("Embedding + storing in bounded windows")
        t0 = time.perf_counter()
        inserted = progressive_insert(
            chunks, context.database.store, context.settings, document_name
        )
        logger.info(
            "pipeline: embedded + stored %d chunks in %.2fs",
            inserted,
            time.perf_counter() - t0,
        )
        progress(
            "Done: %d chunks indexed from %s",
            inserted,
            ChunkStoreFunnel._display_name(document_name),
        )
        return inserted

    @staticmethod
    def _skip(document_name: str, progress: Progress) -> int:
        """Log and report that zero chunks were produced; nothing is stored."""
        logger.warning(
            "pipeline: %s produced zero chunks — keeping any prior document, "
            "storing nothing",
            ChunkStoreFunnel._display_name(document_name),
        )
        progress("No text found — nothing to index")
        return 0

    @staticmethod
    def _display_name(document_name: str) -> str:
        """Return *document_name* redacted for a log/progress line, never stored.

        document_name IS the URL for a plain (unscrubbed) URL ingest, so a
        userinfo/query secret would otherwise reach the log/progress stream
        verbatim (CWE-532). But this funnel is also reached by ingest_content
        with an arbitrary caller-supplied name that is never a URL -- and
        CaptureUrl is unsafe on non-URL input: it silently truncates at the
        first ``#``/``?`` (urlsplit treats them as fragment/query delimiters,
        both valid characters in a plain document name), and some malformed
        strings resembling a URL authority raise ``ValueError`` straight out
        of ``urlsplit``. Gate on the same ``http(s)://`` prefix check
        ``WebFetcher``/``cli_ingest`` already use to recognize a real URL, and
        fall back to *document_name* verbatim -- untruncated, never raising --
        for anything else or for a redaction that still fails despite the
        prefix match.
        """
        if not document_name.lower().startswith(("http://", "https://")):
            return document_name
        try:
            return CaptureUrl(document_name).redacted(lambda text: text)
        except ValueError:
            return document_name

    @staticmethod
    def _build_result(
        document_name: str, inserted: int, stats: IngestStats, collection: str
    ) -> IngestResult:
        """Assemble the IngestResult: identity fields plus format-specific stats."""
        result: IngestResult = {
            "document_name": document_name,
            "collection": collection,
            "chunks": inserted,
        }
        # IngestResult keys are literal-typed, so a variable-key assignment can't be
        # expressed directly; cast the runtime-filtered subset of present fields.
        result.update(cast("IngestResult", stats.as_result_fields()))
        return result


# Module-level alias: pipeline.py, web_ingest.py, and dozens of existing test
# call sites spell this ``_chunk_embed_store(...)`` (no receiver), and DES-036's
# own grep invariant depends on that spelling.  Binding the class's staticmethod
# to a bare module name keeps every call site unchanged while the funnel's
# phases live as real, independently-testable methods on the class above.
_chunk_embed_store = (
    ChunkStoreFunnel._chunk_embed_store  # pyright: ignore[reportPrivateUsage]
)

__all__ = ["ChunkStoreFunnel", "_chunk_embed_store"]
