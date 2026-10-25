"""Backfill historical Claude Code session transcripts into quarry."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.backfill_mapping import ProjectMapping, ProjectMappingResolver
from quarry.backfill_session import BackfillSession
from quarry.db.facade import Database
from quarry.failure_reason import FailureReason
from quarry.ingestion.ingest_context import IngestContext, Progress
from quarry.ingestion.web_ingest import InlineIngest, ingest_content
from quarry.scrub import scrub_and_log
from quarry.stored_sessions import StoredSessions
from quarry.sync_registry import SyncRegistry
from quarry.transcript_reader import TranscriptReader

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.transcript_snapshot import TranscriptSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BackfillConfig:
    """Configuration for a transcript backfill run."""

    dry_run: bool = False
    collection_override: str | None = None
    project_filter: str | None = None
    limit: int | None = None

    def reached_by(self, processed: int) -> bool:
        """Return whether *processed* transcripts are as many as ``limit`` allows."""
        return bool(self.limit) and processed >= (self.limit or 0)


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


@final
class _ProjectProcessor:
    """Ingest one project's uncaptured or grown transcripts, tallying the run.

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
    _stored: StoredSessions
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
        self._stored = StoredSessions([])
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
        self._stored = StoredSessions(docs)
        for transcript in transcripts:
            if self._config.reached_by(self._acc.processed):
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
        # Versioned before the read, so growth during the read or the store
        # leaves the row behind the transcript and the next run re-ingests.
        snapshot = self._stored.snapshot(transcript)
        if self._stored.is_current(snapshot):
            self._skipped += 1
            self._acc.skipped_existing += 1
            return
        self._acc.processed += 1
        if self._config.dry_run:
            self._tally_ingested()
            return
        text = TranscriptReader(transcript).text()
        if not text.strip():
            self._acc.skipped_empty += 1
            return
        self._ingest(snapshot, text)

    def _ingest(self, snapshot: TranscriptSnapshot, text: str) -> None:
        """Write the capture file and ingest the transcript through the pipeline."""
        transcript = snapshot.path
        session = BackfillSession(transcript, self._mapping.project_path, text)
        session.write_capture_file()
        try:
            # Scrub via the pipeline choke point: passing a content_scrubber makes
            # ingest_content redact the content AND the metadata, so the chunks
            # honour the DES-036 write-time scrub invariant through the one scrub
            # every scrubbed caller shares — backfill can never drift from it.
            # (The capture .md is scrubbed independently by CaptureWriter.)
            ingest_content(
                InlineIngest(
                    session.body,
                    format_hint="plain",
                    content_scrubber=lambda t: scrub_and_log(t, "backfill"),
                ),
                session.name,
                Progress(None),
                IngestContext(
                    self._runtime.database,
                    self._runtime.settings,
                    overwrite=True,
                    collection=self._collection,
                    source_version=snapshot.version,
                ),
            )
        except Exception as exc:
            self._acc.errors.append(FailureReason.line(transcript.name, exc))
            logger.exception("backfill: failed to ingest %s", transcript.name)
            return
        self._stored.record(snapshot)
        self._tally_ingested()

    def _tally_ingested(self) -> None:
        """Count one transcript as ingested, for the project and for the run."""
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

    for mapping in mappings:
        _ProjectProcessor(mapping, runtime, acc, config).process()
        if config.reached_by(acc.processed):
            logger.info("backfill: reached limit of %d transcripts", config.limit)
            break

    return BackfillStats(
        ingested=acc.ingested,
        skipped_existing=acc.skipped_existing,
        skipped_unregistered=skipped_unregistered,
        skipped_empty=acc.skipped_empty,
        errors=acc.errors,
    )
