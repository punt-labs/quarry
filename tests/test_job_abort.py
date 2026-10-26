"""JobAbort: aborted jobs fail with their truthful reason, spooled off the loop."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast, final
from unittest.mock import patch

from quarry.daemon.abort_reason import AbortTally
from quarry.daemon.job_abort import JobAbort, QueuedJob
from quarry.daemon.job_spool import JobSpool, SpoolRecord
from quarry.daemon.tasks import TaskState

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.daemon.context import DaemonContext


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


async def test_fail_in_flight_survives_a_repeated_cancel_and_still_records(
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
        running.cancel()
        await asyncio.sleep(0.01)
        release.set()
        await running

    assert job.state.status == "failed"
    assert tally.summary().startswith("1 job(s): 1 spooled for recovery")
