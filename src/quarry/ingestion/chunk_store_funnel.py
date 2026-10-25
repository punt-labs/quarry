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

from quarry.display_name import DisplayName
from quarry.ingestion.run_context import RunContext
from quarry.ingestion.streaming import DocumentStreamer

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
        chunks = ChunkStoreFunnel._chunk(extracted, context)
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
    def _chunk(extracted: ExtractedDocument, context: IngestContext) -> list[Chunk]:
        """Chunk the extracted pages, labelled for this ingest."""
        t0 = time.perf_counter()
        chunks = DocumentStreamer(context.settings).build_chunks(
            extracted.pages, DocumentStreamer.ingest_labels(extracted, context)
        )
        logger.info(
            "pipeline: chunked %d pages → %d chunks in %.2fs",
            len(extracted.pages),
            len(chunks),
            time.perf_counter() - t0,
        )
        return chunks

    @staticmethod
    def _replace_prior(chunks: list[Chunk], context: IngestContext) -> list[str]:
        """Delete the document's earlier chunks, if overwriting; return its twins.

        A copy an older build stored under a former spelling of the same URL is
        replaced too, overwriting or not, when this document was fetched from a
        URL, so a re-ingest leaves one document, not two.  That copy is returned,
        not deleted: it goes only once the new document is stored.

        Every chunk of a document carries its name.  Whether it was fetched from a
        URL is the caller's fact, carried on the *context*, not read back from the
        document's path: a label that merely looks like a URL is not a page.
        """
        return RunContext.legacy_names_of(context).replace_prior(
            chunks[0].document_name, context
        )

    @staticmethod
    def _embed_and_store(
        chunks: list[Chunk],
        document_name: str,
        progress: Progress,
        context: IngestContext,
    ) -> int:
        """Delete the prior document (if overwriting), embed and store, then drop
        any legacy-named copy the stored document replaces."""
        twins = ChunkStoreFunnel._replace_prior(chunks, context)
        progress("Embedding + storing in bounded windows")
        t0 = time.perf_counter()
        inserted = DocumentStreamer(context.settings).insert(
            chunks, context.database.store, document_name
        )
        RunContext.note_stored(context, document_name)
        RunContext.legacy_names_of(context).drop_legacy(
            chunks[0].document_name, twins, context.collection
        )
        logger.info(
            "pipeline: embedded + stored %d chunks in %.2fs",
            inserted,
            time.perf_counter() - t0,
        )
        progress(
            "Done: %d chunks indexed from %s",
            inserted,
            DisplayName(document_name).redacted(),
        )
        return inserted

    @staticmethod
    def _skip(document_name: str, progress: Progress) -> int:
        """Log and report that zero chunks were produced; nothing is stored."""
        logger.warning(
            "pipeline: %s produced zero chunks — keeping any prior document, "
            "storing nothing",
            DisplayName(document_name).redacted(),
        )
        progress("No text found — nothing to index")
        return 0

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
