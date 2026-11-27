"""Post-sync finalization: index rebuild, optimize, capture-shadow push, and GC."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.finalize_steps import OptimizeStep, ShadowPushStep
from quarry.table_upkeep import TableUpkeep

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.types import LanceDB


@final
class SyncFinalizer:
    """Rewrite a changed table, or keep an unchanged one tidy, then push shadows.

    The table-wide rewrite (the collection index, compaction and the full-text
    rebuild) is write-gated: it runs only when the table changed since its last
    successful finalize, or when this process has not finalized it yet.  Time
    gates the rest.  Sweeping orphan pages comes due on a clock whether or not the
    table changed, and the capture-shadow push runs on every finalize because it
    retries a deferred offline push.  The post-sync GC follows only a rewrite, the
    one step that supersedes index generations.
    """

    __slots__ = ("_db", "_settings", "_upkeep")

    _db: LanceDB
    _settings: Settings
    _upkeep: TableUpkeep

    def __new__(cls, db: LanceDB, settings: Settings) -> Self:
        """Bind index maintenance and shadow pushes to a database and settings."""
        self = super().__new__(cls)
        self._db = db
        self._settings = settings
        self._upkeep = TableUpkeep(db, settings)
        return self

    def is_rewrite_due(self) -> bool:
        """Return whether :meth:`run` would rewrite the table right now."""
        return self._upkeep.needs_rewrite()

    def run(self) -> None:
        """Rewrite the table if it changed, else the cheap pass; push shadows."""
        if self._upkeep.needs_rewrite():
            self._rewrite()
        else:
            self._tidy()
        self._push_shadows()

    def _rewrite(self) -> None:
        """Optimize the table, then settle it clean only up to a prior snapshot.

        The snapshot is taken BEFORE the optimize, and the table is marked clean
        only up to it and only when the optimize succeeded.  A write that lands
        while this runs bumps the generation past the snapshot, so it keeps the
        table dirty for the next finalize.
        """
        snapshot = self._upkeep.snapshot()
        self._optimize()
        self._upkeep.settle_rewrite(snapshot)

    def _tidy(self) -> None:
        self._upkeep.skip_rewrite()

    def _optimize(self) -> None:
        OptimizeStep(self._db, self._settings).run()

    def _push_shadows(self) -> None:
        ShadowPushStep(self._settings).run()
