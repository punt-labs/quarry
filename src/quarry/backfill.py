"""Backfill historical Claude Code session transcripts into quarry."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self, final

from quarry.artifacts import (
    SessionArtifacts,
    extract_artifacts,
    format_artifacts_header,
)
from quarry.backfill_mapping import ProjectMapping, ProjectMappingResolver
from quarry.config import Settings
from quarry.db.facade import Database
from quarry.ingestion.ingest_context import IngestContext, Progress
from quarry.ingestion.web_ingest import InlineIngest, ingest_content
from quarry.scrub import scrub_and_log
from quarry.sync_registry import SyncRegistry
from quarry.transcript import Transcript
from quarry.transcript_reader import TranscriptReader

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BackfillConfig:
    """Configuration for a transcript backfill run."""

    dry_run: bool = False
    collection_override: str | None = None
    project_filter: str | None = None
    limit: int | None = None


# A shared immutable default: a bare name reference in the signature below,
# never a call, so it carries no branch of its own (unlike `config or
# BackfillConfig()`) and keeps backfill_sessions' cyclomatic complexity flat.
_DEFAULT_BACKFILL_CONFIG = BackfillConfig()


@dataclass(frozen=True, slots=True)
class _Runtime:
    """The database and settings shared by every project processed in a run."""

    database: Database
    settings: Settings


@dataclass(frozen=True)
class BackfillStats:
    """Aggregate counts from a backfill run."""

    ingested: int = 0
    skipped_existing: int = 0
    skipped_unregistered: int = 0
    skipped_empty: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class _Accumulator:
    """Mutable counters for the backfill loop."""

    ingested: int = 0
    skipped_existing: int = 0
    skipped_empty: int = 0
    processed: int = 0
    errors: list[str] = field(default_factory=list)


def document_name_for_transcript(transcript_path: Path) -> str:
    """Derive the stable ``session-<id[:8]>`` document name for a transcript."""
    return Transcript(transcript_path).document_name()


def is_already_ingested(
    session_id_prefix: str,
    existing_doc_names: set[str],
) -> bool:
    """Return True if this session's document already exists.

    Hook and backfill both file under the same stable ``session-<id[:8]>`` name,
    so this is a clean exact-name check: a session captured by either path — in
    either order — is skipped rather than re-ingested as a second document.
    """
    return f"session-{session_id_prefix}" in existing_doc_names


def _write_backfill_capture_file(
    project_path: str,
    session_id: str,
    transcript: Path,
    artifacts: SessionArtifacts,
    text: str,
) -> None:
    """Write a backfill session capture via the shared CaptureWriter.

    The writer scrubs secrets, PII, and profanity before any bytes reach the
    git-tracked capture file, and fails silently.
    """
    from quarry.capture import CaptureRequest, CaptureWriter  # noqa: PLC0415

    timestamp = Transcript(transcript).timestamp("%Y-%m-%dT%H:%M:%SZ")
    CaptureWriter().write(
        CaptureRequest(
            project_dir=Path(project_path),
            session_id=session_id,
            timestamp=timestamp,
            artifacts=artifacts,
            text=text,
            label="backfill",
        )
    )


@final
class _ProjectProcessor:
    """Ingest one project's not-yet-captured transcripts, tallying the run.

    Owns the per-project state the old free function threaded through eight
    parameters — the shared database, settings, accumulator, and the derived
    target collection and already-seen document names — so each step is a short
    method instead of one deeply nested loop.
    """

    _mapping: ProjectMapping
    _runtime: _Runtime
    _acc: _Accumulator
    _config: BackfillConfig
    _collection: str
    _existing: set[str]
    _ingested: int
    _skipped: int

    def __new__(
        cls,
        mapping: ProjectMapping,
        runtime: _Runtime,
        acc: _Accumulator,
        config: BackfillConfig,
    ) -> Self:
        self = super().__new__(cls)
        self._mapping = mapping
        self._runtime = runtime
        self._acc = acc
        self._config = config
        self._collection = config.collection_override or mapping.captures_collection
        self._existing = set()
        self._ingested = 0
        self._skipped = 0
        return self

    def process(self) -> None:
        """Ingest every transcript for this project, then log the tally."""
        transcripts = self._mapping.transcript_files()
        if not transcripts:
            return
        docs = self._runtime.database.catalog.list_documents(
            collection_filter=self._collection
        )
        self._existing = {d["document_name"] for d in docs}
        limit = self._config.limit or 0
        for transcript in transcripts:
            if limit > 0 and self._acc.processed >= limit:
                break
            self._handle(transcript)
        logger.info(
            "backfill: %s -> %s (%d new, %d already ingested)",
            self._mapping.project_path,
            self._collection,
            self._ingested,
            self._skipped,
        )

    def _handle(self, transcript: Path) -> None:
        """Skip, dry-run, or ingest one transcript, updating the counters."""
        if is_already_ingested(transcript.stem[:8], self._existing):
            self._skipped += 1
            self._acc.skipped_existing += 1
            return
        if self._config.dry_run:
            self._ingested += 1
            self._acc.ingested += 1
            self._acc.processed += 1
            return
        text = TranscriptReader(transcript).text()
        if not text.strip():
            self._acc.skipped_empty += 1
            self._acc.processed += 1
            return
        self._ingest(transcript, text)
        self._acc.processed += 1

    def _ingest(self, transcript: Path, text: str) -> None:
        """Write the capture file and ingest the transcript through the pipeline."""
        artifacts = extract_artifacts(text)
        _write_backfill_capture_file(
            project_path=self._mapping.project_path,
            session_id=transcript.stem,
            transcript=transcript,
            artifacts=artifacts,
            text=text,
        )
        header = format_artifacts_header(artifacts)
        body = f"{header}\n\n{text}" if header else text
        doc_name = document_name_for_transcript(transcript)
        try:
            # Scrub via the pipeline choke point: passing a content_scrubber makes
            # ingest_content redact the content AND the metadata, so the chunks
            # honour the DES-036 write-time scrub invariant through the one scrub
            # every scrubbed caller shares — backfill can never drift from it.
            # (The capture .md is scrubbed independently by CaptureWriter.)
            ingest_content(
                InlineIngest(
                    body,
                    format_hint="plain",
                    content_scrubber=lambda t: scrub_and_log(t, "backfill"),
                ),
                doc_name,
                Progress(None),
                IngestContext(
                    self._runtime.database,
                    self._runtime.settings,
                    overwrite=True,
                    collection=self._collection,
                ),
            )
        except Exception as exc:
            self._acc.errors.append(f"{transcript.name}: {exc}")
            logger.exception("backfill: failed to ingest %s", transcript.name)
            return
        self._existing.add(doc_name)
        self._ingested += 1
        self._acc.ingested += 1


def backfill_sessions(
    settings: Settings,
    config: BackfillConfig = _DEFAULT_BACKFILL_CONFIG,
) -> BackfillStats:
    """Scan Claude Code project transcripts and ingest into quarry."""
    conn = SyncRegistry(settings.registry_path)
    try:
        registrations = conn.list_registrations()
    finally:
        conn.close()

    all_mappings = ProjectMappingResolver.resolve_all(registrations)
    mappings = ProjectMappingResolver.filter_by_project(
        all_mappings, config.project_filter
    )

    runtime = _Runtime(Database.connect(settings.lancedb_path), settings)
    acc = _Accumulator()
    skipped_unregistered = ProjectMappingResolver.count_unmapped(all_mappings)

    limit = config.limit or 0
    for mapping in mappings:
        _ProjectProcessor(mapping, runtime, acc, config).process()
        if limit > 0 and acc.processed >= limit:
            logger.info("backfill: reached limit of %d transcripts", limit)
            break

    return BackfillStats(
        ingested=acc.ingested,
        skipped_existing=acc.skipped_existing,
        skipped_unregistered=skipped_unregistered,
        skipped_empty=acc.skipped_empty,
        errors=acc.errors,
    )
