"""Document ingestion pipeline: dispatch by format, chunk, embed, store."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from quarry.ingestion.chunk_store_funnel import _chunk_embed_store
from quarry.ingestion.format_strategies import SUPPORTED_EXTENSIONS, resolve_strategy
from quarry.ingestion.ingest_context import IngestContext, Progress
from quarry.ingestion.streaming import DocumentStreamer
from quarry.models import PageType

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from quarry.config import Settings
    from quarry.db import Database
    from quarry.ingestion.ingest_context import MemoryType
    from quarry.models import Chunk, PageContent
    from quarry.results import IngestResult

__all__ = ["SUPPORTED_EXTENSIONS", "ingest_document", "plan_file_chunks"]


def ingest_document(
    file_path: Path,
    database: Database,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> IngestResult:
    """Ingest a document: dispatch to the format-specific extraction strategy.

    Supported formats: PDF, TXT, MD, TEX, DOCX, HTML, PPTX, PNG, JPEG, TIFF,
    BMP, WebP, XLSX, CSV.

    Args:
        file_path: Path to the document.
        database: Quarry database facade.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name. Defaults to
            ``file_path.name``.  Pass a relative path (e.g. from
            ``file_path.relative_to(root)``) to avoid collisions when
            syncing directories with identically-named files in
            subdirectories.
        progress_callback: Optional callable for progress messages.
        agent_handle: Agent that owns this memory (empty for non-agent content).
        memory_type: Memory classification (fact, observation, opinion, procedure).
        summary: One-line summary of the content.

    Returns:
        Dict with ingestion results.

    Raises:
        FileNotFoundError: If file does not exist.
        ValueError: If file format is not supported.
    """
    if not file_path.exists():
        msg = f"File not found: {file_path}"
        raise FileNotFoundError(msg)

    document_name = document_name or file_path.name
    progress = Progress(progress_callback)
    context = IngestContext(
        database,
        settings,
        overwrite=overwrite,
        collection=collection,
        agent_handle=agent_handle,
        # ingest_document's own memory_type stays a plain str (unvalidated public
        # API param); IngestContext's field is the tightened Literal, so the
        # cast marks this specific boundary crossing rather than widening the
        # dataclass field back to str.
        memory_type=cast("MemoryType", memory_type),
        summary=summary,
    )
    strategy = resolve_strategy(file_path.suffix.lower(), settings)
    extracted = strategy.extract(settings, file_path, document_name, progress)
    return _chunk_embed_store(extracted, document_name, progress, context)


def plan_file_chunks(
    file_path: Path,
    settings: Settings,
    *,
    collection: str = "default",
    document_name: str | None = None,
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> tuple[list[Chunk], bool]:
    """Extract and chunk a file for sync, reporting extraction determinism.

    Returns ``(chunks, deterministic)``: the full ordered chunk list (embedding
    deferred) so the sync producer knows the document's total chunk count before
    streaming windows, plus whether the extraction is deterministic so resume can
    decide whether to trust a mid-file watermark (DES-034 §5.3, G3).

    Raises:
        FileNotFoundError: If *file_path* does not exist.
        ValueError: If the file format is not supported.
    """
    if not file_path.exists():
        msg = f"File not found: {file_path}"
        raise FileNotFoundError(msg)
    document_name = document_name or file_path.name
    strategy = resolve_strategy(file_path.suffix.lower(), settings)
    extracted = strategy.extract(settings, file_path, document_name, Progress.silent())
    deterministic = _pages_are_deterministic(extracted.pages)
    if not extracted.pages:
        return [], deterministic
    chunks = DocumentStreamer(settings).build_chunks(
        extracted.pages,
        collection=collection,
        source_format=extracted.source_format,
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )
    return chunks, deterministic


def _pages_are_deterministic(pages: list[PageContent]) -> bool:
    """Return False if any page came from OCR (``PageType.IMAGE``), else True.

    OCR (rapidocr) re-segments text differently on re-run, so a document with any
    OCR'd page cannot honor a within-file resume watermark (DES-034 §5.3, G3).
    """
    return not any(page.page_type == PageType.IMAGE for page in pages)
