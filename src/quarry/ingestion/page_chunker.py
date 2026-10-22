"""Chunk page text for embedding: guard, split to the row budget, pack sentences."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, final

from quarry.content_guard import BinaryContent
from quarry.db.row_budget import RowBudget
from quarry.ingestion.page_splitter import PageSplitter
from quarry.ingestion.sentence_chunker import SentenceChunker
from quarry.models import Chunk

if TYPE_CHECKING:
    from quarry.models import PageContent


@final
@dataclass(frozen=True, slots=True)
class ChunkLabels:
    """The metadata every chunk of one ingest carries.

    ``source_format`` is a file extension (``".pdf"``) or ``"inline"`` for
    programmatically supplied text.  ``agent_handle`` owns an agent memory (empty
    for non-agent content) and ``memory_type`` is the free-text memory class the
    daemon has already validated.
    """

    collection: str = "default"
    source_format: str = ""
    agent_handle: str = ""
    memory_type: str = ""
    summary: str = ""

    def require_within(self, budget: RowBudget) -> None:
        """Raise :class:`RowBudgetError` if a label is over *budget*'s label cap."""
        budget.require_label("collection", self.collection)
        budget.require_label("agent_handle", self.agent_handle)
        budget.require_label("summary", self.summary)


@final
class PageChunker:
    """Turn a document's pages into chunks that can always be stored.

    A document that is binary is refused with a reason (:class:`BinaryContent`);
    a page larger than the row budget is split, never truncated; and every chunk
    is at most ``max_chars`` characters, so the stored text is the embedded text.
    Each chunk keeps the full text of its page for LLM reference.
    """

    __slots__ = ("_budget", "_guard", "_labels", "_sentences", "_splitter")

    _budget: RowBudget
    _guard: BinaryContent
    _splitter: PageSplitter
    _sentences: SentenceChunker
    _labels: ChunkLabels

    def __new__(
        cls,
        max_chars: int = 1800,
        overlap_chars: int = 200,
        labels: ChunkLabels | None = None,
        budget: RowBudget | None = None,
    ) -> Self:
        budget = budget or RowBudget()
        # A chunk takes up to four bytes a character.  The overlap a chunk opens
        # with is counted inside max_chars, so max_chars alone must fit the cap.
        if max_chars > budget.max_chunk_chars:
            msg = (
                f"chunk size {max_chars} characters can exceed the "
                f"{budget.max_chunk_bytes:,}-byte chunk budget"
            )
            raise ValueError(msg)
        self = super().__new__(cls)
        self._budget = budget
        self._guard = BinaryContent()
        self._splitter = PageSplitter(budget.max_page_bytes)
        self._sentences = SentenceChunker(max_chars, overlap_chars)
        self._labels = labels or ChunkLabels()
        self._labels.require_within(budget)
        return self

    def chunk(self, pages: list[PageContent]) -> list[Chunk]:
        """Return the chunks of *pages*, numbered contiguously across the document.

        Every row is checked against the budget here, before the caller deletes or
        writes anything, so a violation fails one document without side effects.

        Raises:
            UnindexableContentError: The pages are binary, or a paginated page is
                over the page budget.
            RowBudgetError: A page or chunk exceeds the row budget.
        """
        self._guard.require_document([page.text for page in pages])
        now = datetime.now(tz=UTC)
        chunks: list[Chunk] = []
        for page in self._splitter.expand(pages):
            text = page.text.strip()
            if not text:
                continue
            self._budget.require_page(page.text)
            self._budget.require_label("document_name", page.document_name)
            self._budget.require_label("document_path", page.document_path)
            for piece in self._sentences.split(text):
                self._budget.require_chunk(piece)
                chunks.append(self._chunk(page, len(chunks), piece, now))
        return chunks

    def _chunk(
        self, page: PageContent, index: int, text: str, timestamp: datetime
    ) -> Chunk:
        labels = self._labels
        return Chunk(
            document_name=page.document_name,
            document_path=page.document_path,
            collection=labels.collection,
            page_number=page.page_number,
            total_pages=page.total_pages,
            chunk_index=index,
            text=text,
            page_raw_text=page.text,
            page_type=page.page_type.stored,
            source_format=labels.source_format,
            ingestion_timestamp=timestamp,
            agent_handle=labels.agent_handle,
            memory_type=labels.memory_type,
            summary=labels.summary,
        )
