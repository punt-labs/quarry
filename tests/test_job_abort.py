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

import pytest

from quarry.daemon.abort_reason import Aborted, AbortReason
from quarry.daemon.abort_tally import AbortTally, AbortTerm
from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.job_abort import JobAbort
from quarry.daemon.job_spool import JobSpool, SpoolRecord
from quarry.daemon.queued_unit import QueuedUnit
from quarry.daemon.tasks import TaskState
from quarry.shutdown_policy import ShutdownPolicy
from tests.job_states import JobStates

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskStatus


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


def _job(name: str, *, spooled: bool = True) -> QueuedUnit:
    record = SpoolRecord("remember", "c", name, f"payload {name}") if spooled else None
    return QueuedUnit(_Unit(record), TaskState(task_id=name, kind="remember"))


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


def _exploding(name: str) -> QueuedUnit:
    return QueuedUnit(
        cast("IngestUnit", _Exploding()), TaskState(task_id=name, kind="remember")
    )


def test_one_job_that_cannot_decide_its_outcome_costs_only_itself(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    abort, tally = _abort(tmp_path)
    jobs = [_exploding("bad"), _job("good")]

    with caplog.at_level(logging.ERROR, logger="quarry.daemon"):
        abort.fail(jobs)

    assert [job.state.status for job in jobs] == ["failed", "failed"]
    assert "spooled for recovery" in jobs[1].state.error
    assert tally.summary().startswith("2 job(s): 1 spooled for recovery")
    assert "1 unbuilt" in tally.summary()
    assert "_Exploding job for c" in caplog.text
    assert "spool record could not be built" in jobs[0].state.error


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

    def recording(self: JobSpool, record: SpoolRecord) -> Path | None:
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

    def slow(self: JobSpool, record: SpoolRecord) -> Path | None:
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

    def slow(self: JobSpool, record: SpoolRecord) -> Path | None:
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

    def blocked(self: JobSpool, record: SpoolRecord) -> Path | None:
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
    assert "1 spool unfinished at shutdown" in tally.summary()
    assert "0 not written" in tally.summary()
    assert "0 unbuilt" in tally.summary()
    assert abort.pending == 0


async def test_a_spool_that_raises_is_recorded_lost_and_never_raises(
    tmp_path: Path,
) -> None:
    abort, tally = _abort(tmp_path)
    job = _job("a")

    with patch.object(JobSpool, "write", side_effect=OSError(errno.EIO, "disk")):
        await abort.fail_in_flight(job)

    assert job.state.status == "failed"
    assert "1 unbuilt" in tally.summary()


async def test_a_cancelled_spooling_task_is_unfinished_not_a_failed_write(
    tmp_path: Path,
) -> None:
    abort, _tally = _abort(tmp_path)
    spooling = asyncio.get_running_loop().create_future()
    spooling.cancel()

    outcome = await abort.await_spool(spooling, _job("a"))

    assert outcome is AbortReason.UNFINISHED


async def test_a_late_successful_write_corrects_the_reason_and_the_tally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ShutdownPolicy, "SPOOL_WAIT_S", 0.05)
    abort, tally = _abort(tmp_path)
    job = _job("a")
    release = threading.Event()
    real_write = JobSpool.write

    def late(self: JobSpool, record: SpoolRecord) -> Path | None:
        release.wait(5)
        return real_write(self, record)

    with (
        patch.object(JobSpool, "write", late),
        caplog.at_level(logging.INFO, logger="quarry.daemon"),
    ):
        await abort.fail_in_flight(job)
        assert "1 spool unfinished at shutdown" in tally.summary()
        release.set()
        for _ in range(100):
            if "1 spool unfinished" not in tally.summary():
                break
            await asyncio.sleep(0.01)

    assert tally.summary().startswith("1 job(s): 1 spooled for recovery")
    assert "unfinished" not in tally.summary()
    assert "spooled for recovery" in job.state.error
    assert "after the wait" in job.state.error
    assert "_Unit job for c finished spooling after the abort wait" in caplog.text


async def test_a_late_spool_that_raises_or_is_cancelled_is_logged_by_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ShutdownPolicy, "SPOOL_WAIT_S", 0.05)
    abort, tally = _abort(tmp_path)

    async def raises(_self: DaemonThreads, _call: object) -> Aborted:
        await asyncio.sleep(0.15)
        msg = "late disk failure detail"
        raise RuntimeError(msg)

    async def cancelled(_self: DaemonThreads, _call: object) -> Aborted:
        await asyncio.sleep(0.15)
        raise asyncio.CancelledError

    with caplog.at_level(logging.INFO, logger="quarry.daemon"):
        for fake in (raises, cancelled):
            with patch.object(DaemonThreads, "run", fake):
                await abort.fail_in_flight(_job("a"))
            await asyncio.sleep(0.25)

    assert "a late spool of a _Unit job for c failed" in caplog.text
    assert "a late spool of a _Unit job for c was cancelled" in caplog.text
    assert "late disk failure detail" not in caplog.text
    # The write that raised is settled as not written; the one whose future was
    # cancelled may still land, so that job stays unfinished.
    assert "1 spool unfinished at shutdown" in tally.summary()
    assert "1 not written (spool write failed)" in tally.summary()
    assert abort.pending == 0


async def test_the_summary_terms_add_up_with_every_kind_present(
    tmp_path: Path,
) -> None:
    abort, tally = _abort(tmp_path)
    abort.fail([_job("a"), _job("b", spooled=False), _exploding("c")])
    unfinished = object()
    tally.take(unfinished)
    tally.move(unfinished, AbortTerm.UNFINISHED)

    summary = tally.summary()

    assert summary.startswith("4 job(s)")
    assert "1 spooled for recovery" in summary
    assert "1 unbuilt" in summary
    assert "1 spool unfinished at shutdown" in summary
    assert "1 needed no spool" in summary


@pytest.mark.parametrize(
    ("status", "term", "label"),
    [
        ("withdrawn", AbortTerm.KEPT, "1 kept"),
        ("aborted", AbortTerm.KEPT, "1 kept"),
        ("completed", AbortTerm.KEPT, "1 kept"),
        ("committing", AbortTerm.UNCONFIRMED, "1 unconfirmed"),
    ],
)
def test_a_withdrawn_stored_or_committing_job_keeps_its_status_and_is_not_spooled(
    tmp_path: Path, status: TaskStatus, term: AbortTerm, label: str
) -> None:
    abort, tally = _abort(tmp_path)
    job = QueuedUnit(
        _Unit(SpoolRecord("remember", "c", "a", "p")), JobStates.reaching(status)
    )

    abort.fail([job])

    assert job.state.status == status
    assert job.state.error == ""
    assert tally.term_of(job) is term
    assert label in tally.summary()
    assert not (tmp_path / "spool").exists()
    assert abort.pending == 0


def test_a_job_taken_twice_is_counted_once(tmp_path: Path) -> None:
    abort, tally = _abort(tmp_path)
    job = _job("a")

    abort.fail([job])
    abort.fail([job])

    assert tally.summary().startswith("1 job(s)")


async def test_a_cancelled_queued_spool_is_unfinished_and_corrected_when_it_ends(
    tmp_path: Path,
) -> None:
    abort, tally = _abort(tmp_path)
    job = _job("a")
    started, release = threading.Event(), threading.Event()
    real_write = JobSpool.write

    def blocked(self: JobSpool, record: SpoolRecord) -> Path | None:
        started.set()
        release.wait(5)
        return real_write(self, record)

    with patch.object(JobSpool, "write", blocked):
        running = asyncio.ensure_future(abort.fail_off_loop([job]))
        while not started.is_set():
            await asyncio.sleep(0.005)
        running.cancel()
        await asyncio.wait({running})
        assert tally.term_of(job) is AbortTerm.UNFINISHED
        assert abort.pending == 0
        release.set()
        while tally.term_of(job) is AbortTerm.UNFINISHED:
            await asyncio.sleep(0.005)

    assert tally.term_of(job) is AbortTerm.SPOOLED
    assert "spooled for recovery" in job.state.error


async def test_spool_writes_run_on_daemon_threads_not_the_default_executor(
    tmp_path: Path,
) -> None:
    abort, _tally = _abort(tmp_path)
    seen: list[tuple[bool, str]] = []
    real_write = JobSpool.write

    def recording(self: JobSpool, record: SpoolRecord) -> Path | None:
        thread = threading.current_thread()
        seen.append((thread.daemon, thread.name))
        return real_write(self, record)

    with patch.object(JobSpool, "write", recording):
        await abort.fail_off_loop([_job("a")])

    assert seen
    assert all(daemon and name == "quarry-spool" for daemon, name in seen)
