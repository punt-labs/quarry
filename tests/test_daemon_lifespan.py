"""The daemon lifespan teardown waits for running background tasks, then stops them."""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar, cast

import pytest
from starlette.applications import Starlette

from quarry.daemon.backfill_task import BackfillTask
from quarry.daemon.server import DaemonServer, ServeConfig
from quarry.redacted_failure import RedactedFailure
from quarry.shutdown_policy import ShutdownPolicy
from quarry.stop_signal import StopSignal
from tests.queue_fixtures import ctx_with_queue

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.config import Settings
    from quarry.daemon.context import DaemonContext
    from quarry.run_dir import RunDir


async def _no_op() -> None:
    return None


def _server(tmp_path: Path) -> DaemonServer:
    settings = cast("Settings", SimpleNamespace(lancedb_path=tmp_path / "lancedb"))
    return DaemonServer(settings, ServeConfig(api_key="k"))


def _context(tmp_path: Path, *, budget_s: float = 30.0) -> DaemonContext:
    """A context with a real queue and task registry; the watch loop is a no-op."""
    ctx = ctx_with_queue(tmp_path)
    bound = cast("SimpleNamespace", ctx)
    bound.settings.ingest_drain_timeout_s = budget_s
    bound.start_watch_loop = _no_op
    bound.aclose_watch_loop = _no_op
    return ctx


async def _tear_down(tmp_path: Path, ctx: DaemonContext) -> None:
    app = Starlette()
    app.state.ctx = ctx
    async with _server(tmp_path)._lifespan(app):
        pass


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

    await _tear_down(tmp_path, ctx)

    assert steps == ["stopped", "returned"]  # the queue closing stopped it, and
    assert ctx.tasks.tracked() == []  # shutdown waited for it to return


async def test_a_straggler_is_logged_and_teardown_still_ends_inside_the_budget(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = _context(tmp_path, budget_s=1.0)
    state = ctx.tasks.begin("backfill")
    state.holds_thread = True  # its scan thread will not return
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                continue  # a body that swallows its cancellation

    task = asyncio.ensure_future(stubborn())
    ctx.tasks.track(state, task)
    started = time.monotonic()

    with caplog.at_level(logging.ERROR):
        await _tear_down(tmp_path, ctx)

    assert time.monotonic() - started < 5.0  # one budget, not budget plus a sleep
    [line] = [
        r for r in caplog.records if "still running at shutdown" in r.getMessage()
    ]
    assert state.task_id in line.getMessage()
    assert "backfill" in line.getMessage()
    release.set()
    await task


async def test_a_failing_watch_loop_stop_does_not_skip_the_rest_of_the_teardown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = _context(tmp_path)
    state = ctx.tasks.begin("backfill")
    state.holds_thread = True
    finished = asyncio.Event()

    async def finishes() -> None:
        finished.set()

    async def broken_stop() -> None:
        msg = "the watch loop is broken"
        raise RuntimeError(msg)

    cast("SimpleNamespace", ctx).aclose_watch_loop = broken_stop
    ctx.tasks.track(state, asyncio.ensure_future(finishes()))
    server = _server(tmp_path)
    run_dir = SimpleNamespace(
        port_file=SimpleNamespace(remove=_Recorder("port")),
        token_file=SimpleNamespace(remove=_Recorder("token")),
    )
    server._run_dir = cast("RunDir", run_dir)
    server._bound = True
    app = Starlette()
    app.state.ctx = ctx

    with caplog.at_level(logging.ERROR):
        async with server._lifespan(app):
            pass

    assert finished.is_set()  # the task drain still ran
    assert "teardown: watch-loop stop failed" in caplog.text
    assert "the watch loop is broken" not in caplog.text  # class and frames only
    assert "still running at shutdown" in caplog.text  # the straggler loop ran
    assert _Recorder.calls == ["port", "token"]  # and so did the sidecar removal
    _Recorder.calls.clear()


async def test_a_watch_loop_stop_that_hangs_is_cut_to_its_share(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = _context(tmp_path, budget_s=1.0)

    async def hangs() -> None:
        await asyncio.sleep(30)

    cast("SimpleNamespace", ctx).aclose_watch_loop = hangs
    started = time.monotonic()

    with caplog.at_level(logging.WARNING):
        await _tear_down(tmp_path, ctx)

    assert time.monotonic() - started < 1.0  # the share, not the sleep
    assert "did not stop within its" in caplog.text


async def test_an_exhausted_budget_is_logged_and_the_queue_still_gets_its_share(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = _context(tmp_path, budget_s=0.4)
    windows: list[float] = []

    async def aclose(*, drain_timeout: float) -> None:
        windows.append(drain_timeout)

    cast("SimpleNamespace", ctx).ingest_queue = SimpleNamespace(aclose=aclose)

    async def spends_the_budget() -> None:
        await asyncio.sleep(0.15)  # inside the watch share, but not free

    cast("SimpleNamespace", ctx).aclose_watch_loop = spends_the_budget
    policy = ShutdownPolicy(0.4)

    with caplog.at_level(logging.WARNING):
        await _tear_down(tmp_path, ctx)

    assert windows[0] >= policy.queue_min_s
    assert windows[0] <= policy.total_s


def test_the_scan_bound_is_a_share_of_the_budget_and_never_over_its_cap() -> None:
    small = ShutdownPolicy(10.0)
    large = ShutdownPolicy(10_000.0)

    assert small.scan_bound_s == small.total_s * ShutdownPolicy.SCAN_SHARE
    assert large.scan_bound_s == ShutdownPolicy.SCAN_CAP_S


def test_a_backfill_task_derives_its_scan_bound_from_the_budget(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path, budget_s=12.0)

    bound = BackfillTask(ctx)._bound_s

    assert bound == ShutdownPolicy(12.0).scan_bound_s
    assert BackfillTask(ctx, bound_s=0.3)._bound_s == 0.3


class _Recorder:
    """A sidecar ``remove`` that notes it ran."""

    calls: ClassVar[list[str]] = []

    def __init__(self, name: str) -> None:
        self._name = name

    def __call__(self) -> None:
        self.calls.append(self._name)


async def test_a_cancel_during_a_step_is_logged_and_skips_the_later_steps(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = _context(tmp_path, budget_s=300.0)
    reached: list[str] = []
    entered = asyncio.Event()

    async def blocks() -> None:
        entered.set()
        await asyncio.sleep(30)

    async def drain(*, drain_timeout: float) -> None:
        reached.append("queue")

    cast("SimpleNamespace", ctx).aclose_watch_loop = blocks
    cast("SimpleNamespace", ctx).ingest_queue = SimpleNamespace(aclose=drain)
    # The watch stop is bounded by its own timeout; the cancel under test is the
    # outer one, as when the daemon's own shutdown is cancelled.
    teardown = asyncio.create_task(_tear_down(tmp_path, ctx))
    await entered.wait()

    with caplog.at_level(logging.ERROR):
        teardown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await teardown

    assert "teardown: CancelledError during watch-loop stop; later steps skipped" in (
        caplog.text
    )
    assert reached == []  # the queue drain did not run


def test_uvicorn_is_given_the_graceful_phase_the_margin_leaves(
    tmp_path: Path,
) -> None:
    config = _server(tmp_path)._uvicorn_config(Starlette())

    assert config.timeout_graceful_shutdown == ShutdownPolicy.GRACEFUL_S


async def test_a_non_cancel_base_exception_is_named_for_what_it_is(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = _context(tmp_path, budget_s=300.0)

    async def exits() -> None:
        raise SystemExit(3)

    cast("SimpleNamespace", ctx).aclose_watch_loop = exits

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
        await _tear_down(tmp_path, ctx)

    assert "SystemExit during watch-loop stop" in caplog.text
    assert "CancelledError" not in caplog.text


async def test_a_failure_a_step_already_logged_is_not_logged_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = _context(tmp_path)

    async def stop_that_logged_its_failure() -> None:
        failure = RuntimeError("detail")
        RedactedFailure.mark_logged(failure)
        raise failure

    cast("SimpleNamespace", ctx).aclose_watch_loop = stop_that_logged_its_failure

    with caplog.at_level(logging.ERROR, logger="quarry.daemon.teardown"):
        await _tear_down(tmp_path, ctx)

    assert "teardown: watch-loop stop failed" not in caplog.text


async def test_an_unmarked_failure_is_still_logged_by_the_step(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = _context(tmp_path)

    async def broken() -> None:
        msg = "detail"
        raise RuntimeError(msg)

    cast("SimpleNamespace", ctx).aclose_watch_loop = broken

    with caplog.at_level(logging.ERROR, logger="quarry.daemon.teardown"):
        await _tear_down(tmp_path, ctx)

    assert caplog.text.count("teardown: watch-loop stop failed") == 1
