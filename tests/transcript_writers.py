"""Writers a test hands backfill in place of the daemon's collection FIFO."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast, final

from quarry.backfill import BackfillRuntime
from quarry.daemon.queued_transcript_writer import QueuedTranscriptWriter
from quarry.stop_signal import StopSignal

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from quarry.backfill_session import BackfillSession
    from quarry.config import Settings
    from quarry.daemon.context import DaemonContext
    from quarry.db.facade import Database


@final
@dataclass(frozen=True, slots=True)
class Written:
    """One transcript a writer was asked to store."""

    collection: str
    name: str
    body: str
    snapshot: datetime


@final
@dataclass(slots=True)
class RecordingWriter:
    """Record every write; raise *error* instead of storing when one is given."""

    error: Exception | None = None
    errors_by_name: dict[str, Exception] = field(default_factory=dict)
    after_write: Callable[[], None] | None = None
    calls: list[Written] = field(default_factory=list)

    def write(
        self, collection: str, session: BackfillSession, snapshot: datetime
    ) -> None:
        """Record the write, run the hook, then fail it if this writer was built to."""
        self.calls.append(Written(collection, session.name, session.body, snapshot))
        error = self.errors_by_name.get(session.name, self.error)
        if self.after_write is not None:
            self.after_write()
        if error is not None:
            raise error


@final
@dataclass(frozen=True, slots=True)
class DirectWriter:
    """Run the real backfill job inline against *database*, with no queue."""

    database: Database
    settings: Settings

    def write(
        self, collection: str, session: BackfillSession, snapshot: datetime
    ) -> None:
        """Store *session* through the same job the daemon queues."""
        ctx = cast(
            "DaemonContext",
            SimpleNamespace(database=self.database, settings=self.settings),
        )
        job = QueuedTranscriptWriter.job_for(collection, session, snapshot)
        job.inner.scrub_and_ingest(ctx)


def runtime_over(
    database: Database,
    writer: RecordingWriter | DirectWriter,
    registry_path: Path,
    stop: StopSignal | None = None,
) -> BackfillRuntime:
    """Return a backfill run reading *database* and writing through *writer*."""
    return BackfillRuntime(database, writer, registry_path, stop or StopSignal())
