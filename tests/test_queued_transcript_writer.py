"""QueuedTranscriptWriter: backfill's writes ride the collection FIFO, and say how."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from quarry.backfill_session import BackfillSession
from quarry.backfill_stats import BackfillStats
from quarry.backfill_writer import BackfillWriteError, WriteOutcome
from quarry.daemon.content_jobs import ScrubbedIngestJob
from quarry.daemon.queued_ingest import QueuePolicy
from quarry.daemon.queued_transcript_writer import QueuedTranscriptWriter
from quarry.daemon.refusal_aware_terminal import RefusalAwareTerminal
from quarry.daemon.route_key import RouteKey
from tests.queue_fixtures import BlockingUnit, admit_like_a_hook, ctx_with_queue

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.daemon.tasks import TaskState

_SNAPSHOT = datetime(2026, 1, 2, tzinfo=UTC)
_QUICK = QueuePolicy(admit_timeout_s=0.2, finish_timeout_s=0.2, grace_s=0.2, reserve=0)


def _session(tmp_path: Path) -> BackfillSession:
    transcript = tmp_path / "abcd1234-0000-0000-0000-000000000000.jsonl"
    transcript.write_text("{}\n")
    return BackfillSession(transcript, str(tmp_path), "the conversation")


def _finishing(status: str, error: str = "") -> object:
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
    writer = QueuedTranscriptWriter(ctx, asyncio.get_running_loop())

    with patch.object(ScrubbedIngestJob, "run", _finishing("completed")):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    await ctx.ingest_queue.aclose(drain_timeout=2.0)


async def test_a_transcript_the_worker_failed_raises_the_workers_reason(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    writer = QueuedTranscriptWriter(ctx, asyncio.get_running_loop())

    with (
        patch.object(ScrubbedIngestJob, "run", _finishing("failed", "no text")),
        pytest.raises(BackfillWriteError, match="no text") as raised,
    ):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    await ctx.ingest_queue.aclose(drain_timeout=2.0)
    assert raised.value.outcome is WriteOutcome.FAILED


async def test_a_transcript_with_nothing_to_index_is_told_apart(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path)
    writer = QueuedTranscriptWriter(ctx, asyncio.get_running_loop())
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
    writer = QueuedTranscriptWriter(ctx, asyncio.get_running_loop(), _QUICK)

    with pytest.raises(BackfillWriteError) as raised:
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    assert raised.value.outcome is WriteOutcome.NOT_ADMITTED

    gate.set()
    await ctx.ingest_queue.aclose(drain_timeout=2.0)


async def test_a_shutting_down_daemon_aborts_the_run(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path)
    await ctx.ingest_queue.aclose(drain_timeout=1.0)
    writer = QueuedTranscriptWriter(ctx, asyncio.get_running_loop())

    with pytest.raises(BackfillWriteError, match="daemon shutting down") as raised:
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    assert raised.value.outcome is WriteOutcome.ABORTED


async def test_a_job_that_never_finishes_fails_the_transcript_not_the_run(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    writer = QueuedTranscriptWriter(ctx, asyncio.get_running_loop(), _QUICK)

    async def silent(_self: ScrubbedIngestJob, _ctx: object, _state: object) -> None:
        return None

    with (
        patch.object(ScrubbedIngestJob, "run", silent),
        pytest.raises(BackfillWriteError, match="did not finish within") as raised,
    ):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    await ctx.ingest_queue.aclose(drain_timeout=1.0)
    assert raised.value.outcome is WriteOutcome.FAILED


def test_a_closed_loop_aborts_the_run_at_once(tmp_path: Path) -> None:
    loop = asyncio.new_event_loop()
    loop.close()
    writer = QueuedTranscriptWriter(ctx_with_queue(tmp_path), loop)

    with pytest.raises(BackfillWriteError, match="event loop closed") as raised:
        writer.write("c", _session(tmp_path), _SNAPSHOT)
    assert raised.value.outcome is WriteOutcome.ABORTED


def test_a_stopped_loop_aborts_the_run_instead_of_blocking_the_thread(
    tmp_path: Path,
) -> None:
    loop = asyncio.new_event_loop()  # never run: nothing will ever answer
    writer = QueuedTranscriptWriter(ctx_with_queue(tmp_path), loop, _QUICK)

    with pytest.raises(BackfillWriteError, match="not responding") as raised:
        writer.write("c", _session(tmp_path), _SNAPSHOT)
    assert raised.value.outcome is WriteOutcome.ABORTED

    loop.run_until_complete(asyncio.sleep(0))  # let the cancelled call unwind
    loop.close()


async def test_a_backfill_task_writes_through_the_fifo_and_reports_the_stats(
    tmp_path: Path,
) -> None:
    from quarry.backfill import BackfillRuntime
    from quarry.daemon.backfill_task import BackfillTask
    from quarry.daemon.tasks import TaskState as State

    ctx = ctx_with_queue(tmp_path)
    seen: list[BackfillRuntime] = []

    def fake_backfill(_config: object, runtime: BackfillRuntime) -> BackfillStats:
        seen.append(runtime)
        return BackfillStats(ingested=2)

    state = State(task_id="t", kind="backfill")
    with patch("quarry.backfill.backfill_sessions", fake_backfill):
        await BackfillTask(ctx).run(
            state, dry_run=False, collection="", project="", limit=0
        )

    assert state.status == "completed"
    assert state.results["ingested"] == 2
    assert isinstance(seen[0].writer, QueuedTranscriptWriter)
    assert seen[0].registry_path == ctx.settings.registry_path


async def test_a_drain_aborted_backfill_job_fails_with_a_rerun_hint_and_is_not_spooled(
    tmp_path: Path,
) -> None:
    from quarry.daemon.tasks import TaskState as State

    ctx = ctx_with_queue(tmp_path)
    gate = asyncio.Event()
    admit_like_a_hook(ctx, BlockingUnit(gate))
    job = QueuedTranscriptWriter.job_for("proj-captures", _session(tmp_path), _SNAPSHOT)
    state = State(task_id="queued", kind="backfill")
    assert ctx.ingest_queue.try_submit(RouteKey("db", "proj-captures"), job, state)

    ctx.ingest_queue.cancel_workers()  # the drain gave up: the queued job never ran

    assert state.status == "failed"
    assert state.error == "daemon shut down before ingest completed; re-run backfill"
    assert not any(tmp_path.rglob("*.json"))  # nothing of the conversation was spooled
    gate.set()
