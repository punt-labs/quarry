"""A daemon context with a real ingest queue and task registry, over fake settings."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast, final

from quarry.daemon.ingest_queue import IngestQueue
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.queued_ingest import DEFAULT_QUEUE_POLICY, QueuePolicy
from quarry.daemon.queued_transcript_writer import QueuedTranscriptWriter
from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import TaskRegistry, TaskState
from quarry.daemon.terminal import Terminal
from quarry.daemon.thread_call import ThreadCall
from quarry.stop_signal import StopSignal

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.job_spool import SpoolRecord


def ctx_with_queue(tmp_path: Path, *, depth: int = 32) -> DaemonContext:
    """Return a context with a real queue and registry, over a fake settings."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings = SimpleNamespace(
        ingest_embed_concurrency=1,
        ingest_queue_depth=depth,
        ingest_drain_timeout_s=30.0,
        ingest_max_workers=8,
        ingest_worker_idle_s=60.0,
        quarry_root=Path(tempfile.mkdtemp(dir=tmp_path)),
        registry_path=tmp_path / "registry.db",
    )
    ctx = SimpleNamespace(
        settings=settings, database_name="db", database=object(), tasks=TaskRegistry()
    )
    daemon = cast("DaemonContext", ctx)
    ctx.ingest_queue = IngestQueue(daemon)
    return daemon


@final
@dataclass(slots=True)
class BlockingUnit:
    """A job that holds its collection's worker until *gate* is set."""

    gate: asyncio.Event
    collection: str = "proj-captures"

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Wait for the gate, then complete."""
        del ctx
        with Terminal.for_job(state).recording():
            await self.gate.wait()
            JobLifecycle(state).settle_stored({})

    def spool_record(self) -> SpoolRecord | None:
        """Return no record: nothing here is spooled."""
        return None


def admit_like_a_hook(ctx: DaemonContext, unit: BlockingUnit) -> None:
    """Admit *unit* the way a hook's capture route does: no reserve, no waiting."""
    state = TaskState(task_id="hook", kind="capture")
    key = RouteKey("db", unit.collection)
    admitted = ctx.ingest_queue.try_submit(key, unit, state)
    assert admitted


def writer_for(
    ctx: DaemonContext,
    loop: asyncio.AbstractEventLoop,
    stop: StopSignal | None = None,
    policy: QueuePolicy = DEFAULT_QUEUE_POLICY,
) -> QueuedTranscriptWriter:
    """Return a backfill writer over *ctx*'s queue whose waits follow *policy*."""
    call = ThreadCall(loop, stop or StopSignal(), policy.total_s)
    return QueuedTranscriptWriter(ctx, call, policy)
