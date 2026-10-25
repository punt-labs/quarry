"""Backfill historical Claude Code session transcripts into quarry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, final

from quarry.backfill_mapping import ProjectMappingResolver
from quarry.backfill_run import BackfillRun
from quarry.stop_signal import StopSignal
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from quarry.backfill_stats import BackfillStats
    from quarry.db.facade import Database
    from quarry.transcript_writer import TranscriptWriter


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
    """What a run reads (database, registry), writes through, and is stopped by.

    ``stop`` is set from another thread (the daemon shutting down, the task
    cancelled) with its reason; the run checks it between transcripts and ends
    with the rest recorded as not attempted.
    """

    database: Database
    writer: TranscriptWriter
    registry_path: Path
    stop: StopSignal = field(default_factory=StopSignal)


@final
class Backfill:
    """The whole backfill: read the registrations, file their transcripts, report."""

    __slots__ = ()

    @staticmethod
    def sessions(config: BackfillConfig, runtime: BackfillRuntime) -> BackfillStats:
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
        stats = BackfillRun.open(config, runtime).execute(
            mappings, ProjectMappingResolver.count_unmapped(all_mappings)
        )
        stats.log(dry_run=config.dry_run)
        return stats
