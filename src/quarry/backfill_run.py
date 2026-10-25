"""One backfill run: the parts every project shares, and the order they act in."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.backfill_outcomes import TranscriptOutcomes
from quarry.backfill_project import ProjectProcessor
from quarry.backfill_tally import BackfillTally
from quarry.backfill_unindexable import UnindexableTranscripts

if TYPE_CHECKING:
    from quarry.backfill import BackfillConfig, BackfillRuntime
    from quarry.backfill_mapping import ProjectMapping
    from quarry.backfill_stats import BackfillStats

logger = logging.getLogger(__name__)

_UNINDEXABLE_FILE = "backfill-unindexable.json"


@final
@dataclass(frozen=True, slots=True)
class BackfillRun:
    """The runtime, config and counters of one run, shared by every project."""

    runtime: BackfillRuntime
    config: BackfillConfig
    tally: BackfillTally
    unindexable: UnindexableTranscripts
    outcomes: TranscriptOutcomes

    @classmethod
    def open(cls, config: BackfillConfig, runtime: BackfillRuntime) -> Self:
        """Return a run with fresh counters and the cache beside the registry."""
        tally = BackfillTally(runtime.stop)
        unindexable = UnindexableTranscripts(
            runtime.registry_path.with_name(_UNINDEXABLE_FILE)
        )
        outcomes = TranscriptOutcomes(tally, unindexable, dry_run=config.dry_run)
        return cls(runtime, config, tally, unindexable, outcomes)

    def execute(
        self, mappings: list[ProjectMapping], skipped_unregistered: int
    ) -> BackfillStats:
        """File every project in turn, save what was learned, and return the stats."""
        self.runtime.progress.attach(self.tally, skipped_unregistered)
        try:
            self._process(mappings)
        finally:
            self._close()
        return self.tally.stats(skipped_unregistered)

    def _process(self, mappings: list[ProjectMapping]) -> None:
        """File every project in *mappings*, until done, limited or halted."""
        for mapping in mappings:
            if self.config.reached_by(self.tally.processed):
                logger.info("backfill: reached limit of %s", self.config.limit)
                break
            ProjectProcessor(mapping, self).process()

    def _close(self) -> None:
        """Save what the run learned; a save that fails is a note, never a fault."""
        unsaved = self.unindexable.save()  # never raises, so it cannot mask a fault
        if unsaved:
            self.tally.note(f"unindexable cache not saved: {unsaved}")
