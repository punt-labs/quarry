"""The daemon lifespan teardown waits for running background tasks, then stops them."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from starlette.applications import Starlette

from quarry.daemon.backfill_task import BackfillTask
from quarry.daemon.bounded_scan import BoundedScan
from quarry.daemon.server import DaemonServer, ServeConfig
from quarry.daemon.task_drain import TaskDrain
from quarry.stop_signal import StopSignal
from tests.queue_fixtures import ctx_with_queue

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.config import Settings
    from quarry.daemon.context import DaemonContext


async def _no_op() -> None:
    return None


def _server(tmp_path: Path) -> DaemonServer:
    settings = cast("Settings", SimpleNamespace(lancedb_path=tmp_path / "lancedb"))
    return DaemonServer(settings, ServeConfig(api_key="k"))


def _context(tmp_path: Path) -> DaemonContext:
    """A context with a real queue and task registry; the watch loop is a no-op."""
    ctx = ctx_with_queue(tmp_path)
    bound = cast("SimpleNamespace", ctx)
    bound.start_watch_loop = _no_op
    bound.aclose_watch_loop = _no_op

    async def aclose_ingest_queue() -> None:
        await ctx.ingest_queue.aclose(drain_timeout=5.0)

    bound.aclose_ingest_queue = aclose_ingest_queue
    return ctx


async def test_teardown_waits_for_a_running_backfill_and_it_is_stopped_first(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    state = ctx.tasks.begin("backfill")
    stop = StopSignal()
    steps: list[str] = []

    async def backfill() -> None:
        with ctx.ingest_queue.gate.stopping(stop):  # as the real task does
            while not stop.is_set():
                await asyncio.sleep(0.01)
            steps.append("stopped")
            await asyncio.sleep(0.2)  # finishing the step it was on
            steps.append("returned")

    ctx.tasks.track(state, asyncio.ensure_future(backfill()))
    await asyncio.sleep(0.05)  # it is running
    app = Starlette()
    app.state.ctx = ctx

    async with _server(tmp_path)._lifespan(app):
        assert not stop.is_set()

    assert steps == ["stopped", "returned"]  # the queue closing stopped it, and
    assert ctx.tasks.tracked() == []  # shutdown waited for it to return


def test_the_cancel_wait_is_derived_from_the_backfill_stop_bound() -> None:
    assert (
        TaskDrain.CANCEL_WAIT_S == BackfillTask.STOP_BOUND_S + BoundedScan.POLL_S + 1.0
    )
    assert TaskDrain.CANCEL_WAIT_S > BackfillTask.STOP_BOUND_S
