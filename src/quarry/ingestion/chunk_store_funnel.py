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
from typing import TYPE_CHECKING, Self, cast, final

from quarry.display_name import DisplayName
from quarry.ingestion.existing_page import ExistingPage
from quarry.ingestion.owned_insert import OwnedInsert
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
    """DES-036's single convergence point: chunk, embed, and store one document."""

    __slots__ = ("_context", "_name", "_progress")

    _name: str
    _progress: Progress
    _context: IngestContext

    def __new__(
        cls, document_name: str, progress: Progress, context: IngestContext
    ) -> Self:
        self = super().__new__(cls)
        self._name = document_name
        self._progress = progress
        self._context = context
        return self

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

        Create-if-absent lives here too: a fetched page whose name, or a legacy
        spelling of it, is already held is skipped before anything is deleted or
        inserted, so no front door has to get that right on its own.
        """
        return ChunkStoreFunnel(document_name, progress, context).run(extracted)

    def run(self, extracted: ExtractedDocument) -> IngestResult:
        """Return the result of storing *extracted*, or of skipping it."""
        self._progress("Chunking")
        chunks = self._chunk(extracted)
        self._progress("Created %d chunks", len(chunks))

        if not chunks:
            return self._build_result(self._skip(), extracted.stats)
        if existing := self._existing():
            return existing.skip()
        return self._build_result(self._embed_and_store(chunks), extracted.stats)

    def _existing(self) -> ExistingPage | None:
        """Return the held page that makes this a skip: a fetched page, not a label."""
        if self._context.url_sourced:
            return ExistingPage.page(self._name, self._context)
        return None

    def _chunk(self, extracted: ExtractedDocument) -> list[Chunk]:
        """Chunk the extracted pages, labelled for this ingest."""
        t0 = time.perf_counter()
        chunks = DocumentStreamer(self._context.settings).build_chunks(
            extracted.pages, DocumentStreamer.ingest_labels(extracted, self._context)
        )
        logger.info(
            "pipeline: chunked %d pages → %d chunks in %.2fs",
            len(extracted.pages),
            len(chunks),
            time.perf_counter() - t0,
        )
        return chunks

    def _replace_prior(self, chunks: list[Chunk]) -> None:
        """Delete what the document replaces: its earlier chunks, if overwriting.

        Every chunk of a document carries its name.
        """
        RunContext.legacy_names_of(self._context).replace_prior(
            chunks[0].document_name,
            self._context.collection,
            overwrite=self._context.overwrite,
        )

    def _embed_and_store(self, chunks: list[Chunk]) -> int:
        """Delete the prior document (if overwriting), store, then drop legacy twins.

        A copy an older build stored under a former spelling of the same URL is
        dropped once the new document is stored, so a failed embed or write leaves
        it in place.  Whether the document was fetched from a URL is the caller's
        fact, carried on the context, not read back from the document's path: a
        label that merely looks like a URL is not a page and has no twins.
        """
        self._progress("Embedding + storing in bounded windows")
        self._replace_prior(chunks)
        t0 = time.perf_counter()
        inserted = OwnedInsert(self._name, self._context).run(chunks)
        RunContext.note_stored(self._context, self._name)
        if self._context.url_sourced:
            RunContext.legacy_names_of(self._context).drop_twins(
                self._name, self._context.collection
            )
        logger.info(
            "pipeline: embedded + stored %d chunks in %.2fs",
            inserted,
            time.perf_counter() - t0,
        )
        self._progress(
            "Done: %d chunks indexed from %s",
            inserted,
            DisplayName(self._name).redacted(),
        )
        return inserted

    def _skip(self) -> int:
        """Log and report that zero chunks were produced; nothing is stored."""
        logger.warning(
            "pipeline: %s produced zero chunks — keeping any prior document, "
            "storing nothing",
            DisplayName(self._name).redacted(),
        )
        self._progress("No text found — nothing to index")
        return 0

    def _build_result(self, inserted: int, stats: IngestStats) -> IngestResult:
        """Assemble the IngestResult: identity fields plus format-specific stats."""
        result: IngestResult = {
            "document_name": self._name,
            "collection": self._context.collection,
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
