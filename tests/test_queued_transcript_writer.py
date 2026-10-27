"""QueuedTranscriptWriter: backfill's writes ride the collection FIFO, and say how."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from quarry.backfill_session import BackfillSession
from quarry.backfill_writer import BackfillWriteError, WriteOutcome
from quarry.daemon.content_jobs import ScrubbedIngestJob
from quarry.daemon.queued_ingest import QueuedIngest, QueuePolicy
from quarry.daemon.queued_job import JobWithdrawnError, QueuedJob
from quarry.daemon.queued_transcript_writer import QueuedTranscriptWriter
from quarry.daemon.refusal_aware_terminal import RefusalAwareTerminal
from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import TaskState
from quarry.daemon.thread_call import ThreadCallAbortedError
from quarry.stop_signal import StopSignal
from tests.queue_fixtures import (
    BlockingUnit,
    admit_like_a_hook,
    ctx_with_queue,
    writer_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.daemon.tasks import TaskStatus

_SNAPSHOT = datetime(2026, 1, 2, tzinfo=UTC)
_QUICK = QueuePolicy(admit_timeout_s=0.2, finish_timeout_s=0.2, grace_s=0.2, reserve=0)


def _session(tmp_path: Path) -> BackfillSession:
    transcript = tmp_path / "abcd1234-0000-0000-0000-000000000000.jsonl"
    transcript.write_text("{}\n")
    return BackfillSession(transcript, str(tmp_path), "the conversation")


def _finishing(status: TaskStatus, error: str = "") -> object:
    async def run(_self: ScrubbedIngestJob, _ctx: object, state: TaskState) -> None:
        state.status = status
        state.error = error

    return run


def test_the_job_files_the_session_over_any_earlier_copy_as_of_the_snapshot(
    tmp_path: Path,
) -> None:
    job = QueuedTranscriptWriter.job_for("proj-captures", _session(tmp_path), _SNAPSHOT)

    assert job.collection == "proj-captures"
    assert job.inner.name == "session-abcd1234"
    assert job.inner.labels.overwrite is True
    assert job.inner.scrub_label == "backfill"
    assert job.inner.captured_at == _SNAPSHOT
    assert job.inner.url_sourced is False


def test_the_job_is_never_spooled_and_says_to_rerun_backfill(tmp_path: Path) -> None:
    job = QueuedTranscriptWriter.job_for("c", _session(tmp_path), _SNAPSHOT)

    assert job.spool_record() is None
    assert job.rerun_hint == "re-run backfill"


async def test_a_stored_transcript_returns(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path)
    writer = writer_for(ctx, asyncio.get_running_loop())

    with patch.object(ScrubbedIngestJob, "run", _finishing("completed")):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    await ctx.ingest_queue.aclose(drain_timeout=2.0)


async def test_a_transcript_the_worker_failed_raises_the_workers_reason(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    writer = writer_for(ctx, asyncio.get_running_loop())

    with (
        patch.object(ScrubbedIngestJob, "run", _finishing("failed", "no text")),
        pytest.raises(BackfillWriteError, match="no text") as raised,
    ):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    await ctx.ingest_queue.aclose(drain_timeout=2.0)
    assert raised.value.outcome is WriteOutcome.FAILED


async def test_a_transcript_with_nothing_to_index_is_told_apart(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path)
    writer = writer_for(ctx, asyncio.get_running_loop())
    nothing = RefusalAwareTerminal.NOTHING_STORED

    with (
        patch.object(ScrubbedIngestJob, "run", _finishing("failed", nothing)),
        pytest.raises(BackfillWriteError) as raised,
    ):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    await ctx.ingest_queue.aclose(drain_timeout=2.0)
    assert raised.value.outcome is WriteOutcome.UNINDEXABLE


async def test_a_full_queue_raises_not_admitted_when_the_wait_runs_out(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path, depth=1)
    gate = asyncio.Event()
    admit_like_a_hook(ctx, BlockingUnit(gate))
    writer = writer_for(ctx, asyncio.get_running_loop(), policy=_QUICK)

    with pytest.raises(BackfillWriteError) as raised:
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    assert raised.value.outcome is WriteOutcome.NOT_ADMITTED

    gate.set()
    await ctx.ingest_queue.aclose(drain_timeout=2.0)


async def test_a_shutting_down_daemon_aborts_the_run(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path)
    await ctx.ingest_queue.aclose(drain_timeout=1.0)
    writer = writer_for(ctx, asyncio.get_running_loop())

    with pytest.raises(BackfillWriteError, match="daemon shutting down") as raised:
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    assert raised.value.outcome is WriteOutcome.ABORTED


async def test_a_job_that_never_finishes_aborts_the_run(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    writer = writer_for(ctx, asyncio.get_running_loop(), policy=_QUICK)

    async def silent(_self: ScrubbedIngestJob, _ctx: object, _state: object) -> None:
        return None

    with (
        patch.object(ScrubbedIngestJob, "run", silent),
        pytest.raises(BackfillWriteError, match="did not finish within") as raised,
    ):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    await ctx.ingest_queue.aclose(drain_timeout=1.0)
    assert raised.value.outcome is WriteOutcome.ABORTED_RUNNING  # it had started


def test_a_closed_loop_aborts_the_run_at_once(tmp_path: Path) -> None:
    loop = asyncio.new_event_loop()
    loop.close()
    writer = writer_for(ctx_with_queue(tmp_path), loop)

    with pytest.raises(BackfillWriteError, match="event loop closed") as raised:
        writer.write("c", _session(tmp_path), _SNAPSHOT)
    assert raised.value.outcome is WriteOutcome.ABORTED


def test_a_stopped_loop_aborts_the_run_instead_of_blocking_the_thread(
    tmp_path: Path,
) -> None:
    loop = asyncio.new_event_loop()  # never run: nothing will ever answer
    writer = writer_for(ctx_with_queue(tmp_path), loop, policy=_QUICK)

    with pytest.raises(BackfillWriteError, match="not responding") as raised:
        writer.write("c", _session(tmp_path), _SNAPSHOT)
    assert raised.value.outcome is WriteOutcome.ABORTED

    loop.run_until_complete(asyncio.sleep(0))  # let the cancelled call unwind
    loop.close()


async def test_a_drain_aborted_backfill_job_fails_with_a_rerun_hint_and_is_not_spooled(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    gate = asyncio.Event()
    admit_like_a_hook(ctx, BlockingUnit(gate))
    job = QueuedTranscriptWriter.job_for("proj-captures", _session(tmp_path), _SNAPSHOT)
    state = TaskState(task_id="queued", kind="backfill")
    assert ctx.ingest_queue.try_submit(RouteKey("db", "proj-captures"), job, state)

    ctx.ingest_queue.cancel_workers()  # the drain gave up: the queued job never ran

    assert state.status == "failed"
    assert state.error == "daemon shut down before ingest completed; re-run backfill"
    assert not any(tmp_path.rglob("*.json"))  # nothing of the conversation was spooled
    gate.set()


async def test_a_cancelled_job_means_the_daemon_is_shutting_down(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    writer = writer_for(ctx, asyncio.get_running_loop())

    async def cancelled(
        _self: QueuedIngest, _job: object, _state: TaskState
    ) -> TaskState:
        raise asyncio.CancelledError

    with (
        patch.object(QueuedIngest, "run_tracked", cancelled),
        pytest.raises(BackfillWriteError, match="daemon shutting down") as raised,
    ):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    assert raised.value.outcome is WriteOutcome.ABORTED


async def test_a_stop_is_honored_within_a_second_whatever_the_policy_allows(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    stop = StopSignal()
    long_policy = QueuePolicy(admit_timeout_s=600.0, finish_timeout_s=600.0, reserve=0)
    writer = writer_for(ctx, asyncio.get_running_loop(), stop, long_policy)
    gate = asyncio.Event()
    admit_like_a_hook(ctx, BlockingUnit(gate))  # the queue is full of a hung job
    asyncio.get_running_loop().call_later(0.2, stop.set, "task cancelled")
    started = time.monotonic()

    with pytest.raises(BackfillWriteError, match="task cancelled") as raised:
        await asyncio.to_thread(
            writer.write, "proj-captures", _session(tmp_path), _SNAPSHOT
        )

    assert time.monotonic() - started < 3.0
    assert raised.value.outcome is WriteOutcome.ABORTED
    gate.set()
    await ctx.ingest_queue.aclose(drain_timeout=2.0)


async def test_a_stop_while_the_job_is_queued_behind_another_never_lets_it_write(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    stop = StopSignal()
    writer = writer_for(ctx, asyncio.get_running_loop(), stop)
    gate = asyncio.Event()
    admit_like_a_hook(ctx, BlockingUnit(gate))  # the worker is held by a hook's job
    wrote: list[str] = []

    async def records(_self: ScrubbedIngestJob, _ctx: object, state: TaskState) -> None:
        wrote.append("written")
        state.status = "completed"

    asyncio.get_running_loop().call_later(0.2, stop.set, "task cancelled")
    with (
        patch.object(ScrubbedIngestJob, "run", records),
        pytest.raises(BackfillWriteError, match="task cancelled") as raised,
    ):
        await asyncio.to_thread(
            writer.write, "proj-captures", _session(tmp_path), _SNAPSHOT
        )
    gate.set()  # the worker is free again: the abandoned job must not run
    await ctx.ingest_queue.aclose(drain_timeout=2.0)

    assert raised.value.outcome is WriteOutcome.ABORTED
    assert wrote == []


async def test_a_stop_while_the_job_is_running_counts_it_as_having_run(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    stop = StopSignal()
    writer = writer_for(ctx, asyncio.get_running_loop(), stop)
    gate = asyncio.Event()

    async def holds(_self: ScrubbedIngestJob, _ctx: object, state: TaskState) -> None:
        await gate.wait()
        state.status = "completed"

    asyncio.get_running_loop().call_later(0.3, stop.set, "task cancelled")
    with (
        patch.object(ScrubbedIngestJob, "run", holds),
        pytest.raises(BackfillWriteError) as raised,
    ):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    gate.set()
    await ctx.ingest_queue.aclose(drain_timeout=2.0)

    assert raised.value.outcome is WriteOutcome.ABORTED_RUNNING


async def test_a_job_that_times_out_still_queued_never_writes_later(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    writer = writer_for(ctx, asyncio.get_running_loop(), policy=_QUICK)
    gate = asyncio.Event()
    admit_like_a_hook(ctx, BlockingUnit(gate))  # a hung worker holds the FIFO
    wrote: list[str] = []

    async def records(_self: ScrubbedIngestJob, _ctx: object, state: TaskState) -> None:
        wrote.append("written")
        state.status = "completed"

    with (
        patch.object(ScrubbedIngestJob, "run", records),
        pytest.raises(BackfillWriteError, match="did not finish within") as raised,
    ):
        await asyncio.to_thread(
            writer.write, "proj-captures", _session(tmp_path), _SNAPSHOT
        )
    gate.set()
    await ctx.ingest_queue.aclose(drain_timeout=2.0)

    assert raised.value.outcome is WriteOutcome.ABORTED
    assert wrote == []


def _aborting_after(status: TaskStatus, error: str = "") -> object:
    """A wait that ended without an answer, just after the job reached *status*."""

    async def run_tracked(
        _self: QueuedIngest, _job: object, state: TaskState
    ) -> TaskState:
        state.status = status
        state.error = error
        msg = "task cancelled"
        raise ThreadCallAbortedError(msg)

    return run_tracked


async def test_a_job_that_stored_just_before_the_withdraw_is_counted_stored(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    writer = writer_for(ctx, asyncio.get_running_loop())

    with patch.object(QueuedIngest, "run_tracked", _aborting_after("completed")):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)


async def test_a_job_past_its_commit_at_the_withdraw_is_unconfirmed(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    writer = writer_for(ctx, asyncio.get_running_loop())

    with (
        patch.object(QueuedIngest, "run_tracked", _aborting_after("committing")),
        pytest.raises(BackfillWriteError) as raised,
    ):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)

    assert raised.value.outcome is WriteOutcome.ABORTED_COMMITTING


async def test_a_job_that_failed_just_before_the_withdraw_fails_with_its_own_reason(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    writer = writer_for(ctx, asyncio.get_running_loop())

    with (
        patch.object(QueuedIngest, "run_tracked", _aborting_after("failed", "no text")),
        pytest.raises(BackfillWriteError, match="no text") as raised,
    ):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)

    assert raised.value.outcome is WriteOutcome.FAILED


async def test_a_running_job_withdrawn_before_its_commit_never_writes(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    stop = StopSignal()
    writer = writer_for(ctx, asyncio.get_running_loop(), stop)
    gate = asyncio.Event()
    wrote: list[str] = []

    async def embeds_then_commits(
        self: ScrubbedIngestJob, _ctx: object, state: TaskState
    ) -> None:
        await gate.wait()  # still embedding when the run is stopped
        assert self.checkpoint is not None
        try:
            self.checkpoint(QueuedJob.COMMIT_POINT)  # raises: withdrawn
        except JobWithdrawnError:
            return  # the job's terminal handler would record the refusal
        wrote.append("written")
        state.status = "completed"

    asyncio.get_running_loop().call_later(0.3, stop.set, "task cancelled")
    with (
        patch.object(ScrubbedIngestJob, "run", embeds_then_commits),
        pytest.raises(BackfillWriteError) as raised,
    ):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    gate.set()
    await asyncio.sleep(0.2)
    await ctx.ingest_queue.aclose(drain_timeout=2.0)

    assert raised.value.outcome is WriteOutcome.ABORTED_RUNNING
    assert wrote == []


async def test_a_job_past_its_commit_when_the_run_stops_is_unconfirmed(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    stop = StopSignal()
    writer = writer_for(ctx, asyncio.get_running_loop(), stop)
    gate = asyncio.Event()
    wrote: list[str] = []

    async def commits_then_writes(
        self: ScrubbedIngestJob, _ctx: object, state: TaskState
    ) -> None:
        assert self.checkpoint is not None
        self.checkpoint(QueuedJob.COMMIT_POINT)  # past the point of no return
        await gate.wait()
        wrote.append("written")
        state.status = "completed"

    asyncio.get_running_loop().call_later(0.3, stop.set, "task cancelled")
    with (
        patch.object(ScrubbedIngestJob, "run", commits_then_writes),
        pytest.raises(BackfillWriteError) as raised,
    ):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    gate.set()
    await asyncio.sleep(0.2)
    await ctx.ingest_queue.aclose(drain_timeout=2.0)

    # neither stored nor lost: the store can still fail after its commit point
    assert raised.value.outcome is WriteOutcome.ABORTED_COMMITTING
    assert wrote == ["written"]


async def test_a_committing_job_that_then_fails_is_never_counted_stored(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    stop = StopSignal()
    writer = writer_for(ctx, asyncio.get_running_loop(), stop)
    gate = asyncio.Event()

    async def commits_then_fails(
        self: ScrubbedIngestJob, _ctx: object, state: TaskState
    ) -> None:
        assert self.checkpoint is not None
        self.checkpoint(QueuedJob.COMMIT_POINT)
        await gate.wait()
        state.status = "failed"
        state.error = "insert failed"

    asyncio.get_running_loop().call_later(0.3, stop.set, "task cancelled")
    with (
        patch.object(ScrubbedIngestJob, "run", commits_then_fails),
        pytest.raises(BackfillWriteError) as raised,
    ):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    gate.set()
    await asyncio.sleep(0.2)
    await ctx.ingest_queue.aclose(drain_timeout=2.0)

    assert raised.value.outcome is WriteOutcome.ABORTED_COMMITTING
