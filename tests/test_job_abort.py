"""JobAbort: aborted jobs fail with their truthful reason, spooled off the loop."""

from __future__ import annotations

import asyncio
import errno
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast, final
from unittest.mock import patch

from quarry.daemon.abort_reason import AbortTally
from quarry.daemon.job_abort import JobAbort, QueuedJob
from quarry.daemon.job_spool import JobSpool, SpoolRecord
from quarry.daemon.tasks import TaskState
from quarry.shutdown_policy import ShutdownPolicy

if TYPE_CHECKING:
    import pytest

    from quarry.config import Settings
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit


@final
@dataclass(frozen=True, slots=True)
class _Unit:
    record: SpoolRecord | None
    collection: str = "c"

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        del ctx, state

    def spool_record(self) -> SpoolRecord | None:
        return self.record


def _abort(tmp_path: Path) -> tuple[JobAbort, AbortTally]:
    tally = AbortTally()
    settings = cast("Settings", SimpleNamespace(quarry_root=tmp_path))
    return JobAbort(settings, tally), tally


def _job(name: str, *, spooled: bool = True) -> QueuedJob:
    record = SpoolRecord("remember", "c", name, f"payload {name}") if spooled else None
    return QueuedJob(_Unit(record), TaskState(task_id=name, kind="remember"))


def test_fail_spools_each_job_and_marks_it_failed_with_its_reason(
    tmp_path: Path,
) -> None:
    abort, tally = _abort(tmp_path)
    jobs = [_job("a"), _job("b", spooled=False)]

    abort.fail(jobs)

    assert [job.state.status for job in jobs] == ["failed", "failed"]
    assert "spooled for recovery" in jobs[0].state.error
    assert len(list((tmp_path / "spool").glob("remember-*.json"))) == 1
    assert tally.summary().startswith("2 job(s): 1 spooled for recovery")
    assert abort.pending == 0


class _Exploding:
    """A unit whose spool record cannot be built."""

    collection = "c"

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        del ctx, state

    def spool_record(self) -> SpoolRecord | None:
        msg = "cannot build the record"
        raise RuntimeError(msg)


def _exploding(name: str) -> QueuedJob:
    return QueuedJob(
        cast("IngestUnit", _Exploding()), TaskState(task_id=name, kind="remember")
    )


def test_one_job_that_cannot_decide_its_outcome_costs_only_itself(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    abort, tally = _abort(tmp_path)
    jobs = [_exploding("bad"), _job("good")]

    with caplog.at_level(logging.ERROR, logger="quarry.daemon.job_abort"):
        abort.fail(jobs)

    assert [job.state.status for job in jobs] == ["failed", "failed"]
    assert "spooled for recovery" in jobs[1].state.error
    assert tally.summary().startswith("2 job(s): 1 spooled for recovery")
    assert "1 lost" in tally.summary()
    assert "_Exploding job for c" in caplog.text


async def test_off_loop_one_bad_job_still_records_the_others(tmp_path: Path) -> None:
    abort, tally = _abort(tmp_path)
    jobs = [_exploding("bad"), _job("good")]

    await abort.fail_off_loop(jobs)

    assert [job.state.status for job in jobs] == ["failed", "failed"]
    assert tally.summary().startswith("2 job(s): 1 spooled for recovery")


async def test_fail_off_loop_spools_on_a_thread_and_records_on_the_loop(
    tmp_path: Path,
) -> None:
    abort, tally = _abort(tmp_path)
    threads: list[int] = []
    real_write = JobSpool.write

    def recording(self: JobSpool, record: SpoolRecord) -> bool:
        threads.append(threading.get_ident())
        return real_write(self, record)

    with patch.object(JobSpool, "write", recording):
        await abort.fail_off_loop([_job("a")])

    assert threads
    assert threading.get_ident() not in threads
    assert tally.summary().startswith("1 job(s): 1 spooled for recovery")


async def test_a_job_is_failed_at_once_and_pending_until_its_spool_ends(
    tmp_path: Path,
) -> None:
    abort, tally = _abort(tmp_path)
    job = _job("a")
    spooling, release = threading.Event(), threading.Event()
    real_write = JobSpool.write

    def slow(self: JobSpool, record: SpoolRecord) -> bool:
        spooling.set()
        release.wait(5)
        return real_write(self, record)

    with patch.object(JobSpool, "write", slow):
        running = asyncio.ensure_future(abort.fail_off_loop([job]))
        while not spooling.is_set():
            await asyncio.sleep(0.005)
        assert job.state.status == "failed"  # terminal before the spool ends
        assert abort.pending == 1
        assert tally.summary().startswith("0 job(s)")
        release.set()
        await running

    assert abort.pending == 0
    assert "spooled for recovery" in job.state.error


async def test_fail_in_flight_survives_repeated_cancels_and_still_records(
    tmp_path: Path,
) -> None:
    abort, tally = _abort(tmp_path)
    job = _job("a")
    spooling, release = threading.Event(), threading.Event()
    real_write = JobSpool.write

    def slow(self: JobSpool, record: SpoolRecord) -> bool:
        spooling.set()
        release.wait(5)
        return real_write(self, record)

    with patch.object(JobSpool, "write", slow):
        running = asyncio.ensure_future(abort.fail_in_flight(job))
        while not spooling.is_set():
            await asyncio.sleep(0.005)
        running.cancel()  # the drain timeout's cancel
        await asyncio.sleep(0.01)
        running.cancel()  # the abort's cancel, while still spooling
        await asyncio.sleep(0.01)
        release.set()
        await running

    assert job.state.status == "failed"
    assert "spooled for recovery" in job.state.error
    assert tally.summary().startswith("1 job(s): 1 spooled for recovery")


async def test_a_spool_that_blocks_past_the_deadline_is_recorded_unfinished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ShutdownPolicy, "SPOOL_WAIT_S", 0.1)
    abort, tally = _abort(tmp_path)
    job = _job("a")
    release = threading.Event()
    real_write = JobSpool.write

    def blocked(self: JobSpool, record: SpoolRecord) -> bool:
        release.wait(5)
        return real_write(self, record)

    started = time.monotonic()
    with patch.object(JobSpool, "write", blocked):
        await abort.fail_in_flight(job)
        elapsed = time.monotonic() - started
        release.set()

    assert elapsed < 1.0
    assert job.state.status == "failed"
    assert "spool unfinished at shutdown" in job.state.error
    assert "1 lost" in tally.summary()
    assert abort.pending == 0


async def test_a_spool_that_raises_is_recorded_lost_and_never_raises(
    tmp_path: Path,
) -> None:
    abort, tally = _abort(tmp_path)
    job = _job("a")

    with patch.object(JobSpool, "write", side_effect=OSError(errno.EIO, "disk")):
        await abort.fail_in_flight(job)

    assert job.state.status == "failed"
    assert "1 lost" in tally.summary()


async def test_a_cancelled_spooling_task_is_recorded_lost(tmp_path: Path) -> None:
    abort, _tally = _abort(tmp_path)
    spooling = asyncio.get_running_loop().create_future()
    spooling.cancel()

    outcome = await abort.await_spool(spooling)

    assert outcome is JobAbort.UNDECIDED
