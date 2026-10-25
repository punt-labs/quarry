"""Backfill historical Claude Code session transcripts into quarry."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.backfill_mapping import ProjectMapping, ProjectMappingResolver
from quarry.backfill_session import BackfillSession
from quarry.failure_reason import FailureReason
from quarry.stored_sessions import StoredSessions
from quarry.sync_registry import SyncRegistry
from quarry.transcript_reader import TranscriptReader

if TYPE_CHECKING:
    from quarry.backfill_writer import TranscriptWriter
    from quarry.db.facade import Database

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


@dataclass(frozen=True, slots=True)
class BackfillRuntime:
    """What a run reads (the database, the registry) and writes through (the writer)."""

    database: Database
    writer: TranscriptWriter
    registry_path: Path


@dataclass(frozen=True)
class BackfillStats:
    """Aggregate counts from a backfill run."""

    ingested: int = 0
    skipped_existing: int = 0
    skipped_unregistered: int = 0
    skipped_empty: int = 0
    errors: list[str] = field(default_factory=list)

    def as_results(self, *, dry_run: bool) -> dict[str, object]:
        """Return the stats as a task's results."""
        return {
            "ingested": self.ingested,
            "skipped_existing": self.skipped_existing,
            "skipped_unregistered": self.skipped_unregistered,
            "skipped_empty": self.skipped_empty,
            "errors": list(self.errors),
            "dry_run": dry_run,
        }


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
    _runtime: BackfillRuntime
    _acc: _Accumulator
    _config: BackfillConfig
    _collection: str
    _stored: StoredSessions
    _ingested: int
    _skipped: int

    def __new__(
        cls,
        mapping: ProjectMapping,
        runtime: BackfillRuntime,
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
        if self._stored.is_current(transcript):
            self._skipped += 1
            self._acc.skipped_existing += 1
            return
        self._acc.processed += 1
        if self._config.dry_run:
            self._tally_ingested()
            return
        # Taken before the read: a write after this instant is newer than what is
        # stored, whatever happens between the read and the store.
        snapshot = datetime.now(UTC)
        text = TranscriptReader(transcript).text()
        if not text.strip():
            self._acc.skipped_empty += 1
            return
        self._ingest(transcript, text, snapshot)

    def _ingest(self, transcript: Path, text: str, snapshot: datetime) -> None:
        """Write the capture file and ingest the transcript through the pipeline."""
        session = BackfillSession(transcript, self._mapping.project_path, text)
        session.write_capture_file()
        try:
            # The writer scrubs through the pipeline choke point (a ``backfill``
            # scrubber redacts content AND metadata), so backfill cannot drift
            # from the write-time scrub invariant.  The capture .md is scrubbed
            # independently by CaptureWriter.
            self._runtime.writer.write(self._collection, session, snapshot)
        except Exception as exc:
            self._acc.errors.append(FailureReason.line(transcript.name, exc))
            logger.exception("backfill: failed to ingest %s", transcript.name)
            return
        self._stored.record(transcript, snapshot)
        self._tally_ingested()

    def _tally_ingested(self) -> None:
        """Count one transcript as ingested, for the project and for the run."""
        self._ingested += 1
        self._acc.ingested += 1


def backfill_sessions(
    config: BackfillConfig, runtime: BackfillRuntime
) -> BackfillStats:
    """Scan Claude Code project transcripts and file them through *runtime*."""
    conn = SyncRegistry(runtime.registry_path)
    try:
        registrations = conn.list_registrations()
    finally:
        conn.close()

    all_mappings = ProjectMappingResolver.resolve_all(registrations)
    mappings = ProjectMappingResolver.filter_by_project(
        all_mappings, config.project_filter
    )

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
