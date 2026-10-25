"""QueuedTranscriptWriter: backfill's writes ride the collection FIFO, and say how."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from quarry.backfill_session import BackfillSession
from quarry.backfill_writer import BackfillWriteError
from quarry.daemon.content_jobs import ScrubbedIngestJob
from quarry.daemon.queued_ingest import IngestNotAdmittedError
from quarry.daemon.queued_transcript_writer import QueuedTranscriptWriter
from tests.queue_fixtures import ctx_with_queue

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.daemon.tasks import TaskState

_SNAPSHOT = datetime(2026, 1, 2, tzinfo=UTC)


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
    assert job.name == "session-abcd1234"
    assert job.labels.overwrite is True
    assert job.scrub_label == "backfill"
    assert job.captured_at == _SNAPSHOT
    assert job.url_sourced is False


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
        pytest.raises(BackfillWriteError, match="no text"),
    ):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    await ctx.ingest_queue.aclose(drain_timeout=2.0)


async def test_a_full_queue_raises_when_the_wait_runs_out(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path, depth=0)
    writer = QueuedTranscriptWriter(
        ctx, asyncio.get_running_loop(), admit_timeout_s=0.2
    )

    with pytest.raises(IngestNotAdmittedError):
        await asyncio.to_thread(writer.write, "c", _session(tmp_path), _SNAPSHOT)
    await ctx.ingest_queue.aclose(drain_timeout=2.0)


async def test_a_backfill_task_writes_through_the_fifo_and_reports_the_stats(
    tmp_path: Path,
) -> None:
    from quarry.backfill import BackfillRuntime, BackfillStats
    from quarry.daemon.backfill_task import BackfillTask

    ctx = ctx_with_queue(tmp_path)
    seen: list[BackfillRuntime] = []

    def fake_backfill(_config: object, runtime: BackfillRuntime) -> BackfillStats:
        seen.append(runtime)
        return BackfillStats(ingested=2)

    with patch("quarry.backfill.backfill_sessions", fake_backfill):
        results = await BackfillTask(ctx).run(
            dry_run=False, collection="", project="", limit=0
        )

    assert results["ingested"] == 2
    assert isinstance(seen[0].writer, QueuedTranscriptWriter)
    assert seen[0].registry_path == ctx.settings.registry_path
