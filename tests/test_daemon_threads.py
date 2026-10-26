"""DaemonThreads: blocking work on daemon threads that never hold the process."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import textwrap
import threading
import time
from functools import partial
from unittest.mock import patch

import pytest

from quarry.daemon.daemon_threads import DaemonThreadAbortedError, DaemonThreads


async def test_a_call_runs_on_a_daemon_thread_off_the_default_executor() -> None:
    threads = DaemonThreads(name="quarry-test")

    thread = await threads.run(threading.current_thread)

    assert thread.daemon
    assert thread.name == "quarry-test"
    assert thread is not threading.current_thread()


async def test_a_calls_failure_is_raised_to_the_caller() -> None:
    def boom() -> None:
        msg = "disk"
        raise OSError(msg)

    with pytest.raises(OSError, match="disk"):
        await DaemonThreads().run(boom)


async def test_no_more_than_the_bound_run_at_once() -> None:
    threads = DaemonThreads()
    running = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()

    def hold() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        release.wait(5)
        with lock:
            running -= 1

    pending = [
        asyncio.ensure_future(threads.run(hold))
        for _ in range(DaemonThreads.SHARED_THREADS + 3)
    ]
    await asyncio.sleep(0.2)
    release.set()
    await asyncio.gather(*pending)

    assert peak == DaemonThreads.SHARED_THREADS


async def test_a_cancelled_caller_does_not_stop_the_work_or_the_pool() -> None:
    threads = DaemonThreads()
    started = threading.Event()
    release = threading.Event()
    outcome: list[str] = []

    def work() -> None:
        started.set()
        release.wait(5)
        outcome.append("done")

    waiting = asyncio.ensure_future(threads.run(work))
    await asyncio.sleep(0.05)
    assert started.is_set()
    waiting.cancel()
    release.set()
    await asyncio.sleep(0.1)

    assert outcome == ["done"]
    assert await asyncio.wait_for(threads.run(lambda: "later"), 2) == "later"


def test_a_call_that_never_returns_does_not_keep_the_process_alive() -> None:
    """With the default executor, asyncio.run would wait for the call to end."""
    script = textwrap.dedent(
        """
        import asyncio, threading
        from quarry.daemon.daemon_threads import DaemonThreads

        async def main():
            try:
                await asyncio.wait_for(
                    DaemonThreads.shared().run(lambda: threading.Event().wait()), 0.2
                )
            except TimeoutError:
                pass

        asyncio.run(main())
        """
    )
    started = time.monotonic()

    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, timeout=30, check=False
    )

    assert done.returncode == 0, done.stderr
    assert time.monotonic() - started < 15


async def test_a_stalled_job_does_not_keep_a_burst_from_running() -> None:
    threads = DaemonThreads(max_threads=3)
    release = threading.Event()
    done: list[int] = []

    def stalls() -> None:
        release.wait(5)

    def quick(n: int) -> None:
        done.append(n)

    stalled = asyncio.ensure_future(threads.run(stalls))
    await asyncio.sleep(0.05)
    await asyncio.wait_for(
        asyncio.gather(*(threads.run(partial(quick, n)) for n in range(5))), 2
    )
    release.set()
    await stalled

    assert sorted(done) == [0, 1, 2, 3, 4]


async def test_a_burst_spawns_up_to_the_bound_not_one_thread() -> None:
    threads = DaemonThreads(max_threads=3)
    names: set[int] = set()
    lock = threading.Lock()
    gate = threading.Barrier(3, timeout=5)

    def meet() -> None:
        with lock:
            names.add(threading.get_ident())
        gate.wait()

    await asyncio.gather(*(threads.run(meet) for _ in range(3)))

    assert len(names) == 3


class _Fatal(BaseException):
    """A BaseException that is neither an Exception nor a cancel."""


async def test_a_base_exception_reaches_the_caller_wrapped_and_the_loop_survives() -> (
    None
):
    threads = DaemonThreads(max_threads=1)

    def exits() -> None:
        sys.exit(3)

    def fatal() -> None:
        raise _Fatal

    for call, original in ((exits, SystemExit), (fatal, _Fatal)):
        with pytest.raises(DaemonThreadAbortedError) as raised:
            await threads.run(call)
        assert isinstance(raised.value.__cause__, original)

    assert await asyncio.wait_for(threads.run(lambda: 7), 2) == 7  # a new thread


async def test_stop_iteration_is_wrapped_because_a_future_refuses_it() -> None:
    def stops() -> None:
        raise StopIteration

    with pytest.raises(DaemonThreadAbortedError) as raised:
        await DaemonThreads(max_threads=1).run(stops)

    assert isinstance(raised.value.__cause__, StopIteration)


async def test_a_thread_that_cannot_start_fails_the_caller_and_leaves_nothing() -> None:
    threads = DaemonThreads(max_threads=1)
    ran: list[str] = []

    def never() -> None:
        ran.append("never")

    with (
        patch.object(threading.Thread, "start", side_effect=RuntimeError("no threads")),
        pytest.raises(RuntimeError, match="no threads"),
    ):
        await threads.run(never)
    result = await asyncio.wait_for(threads.run(lambda: "later"), 2)
    await asyncio.sleep(0.05)

    assert result == "later"  # the pool still serves the next call
    assert ran == []  # and the failed call's work never runs later


async def test_work_stranded_by_a_replacement_that_cannot_start_fails_in_order(
    caplog: pytest.LogCaptureFixture,
) -> None:
    threads = DaemonThreads(max_threads=1)
    release = threading.Event()
    started = threading.Event()
    real_start = threading.Thread.start
    starts: list[str] = []

    def start_once(self: threading.Thread) -> None:
        if starts:
            msg = "no thread for the replacement"
            raise RuntimeError(msg)
        starts.append(self.name)
        real_start(self)

    def dies() -> None:
        started.set()
        release.wait(5)
        sys.exit(1)  # the only thread ends, with work queued behind it

    ended: list[str] = []
    died: list[threading.ExceptHookArgs] = []
    with (
        patch.object(threading.Thread, "start", start_once),
        patch.object(threading, "excepthook", died.append),
        caplog.at_level(logging.WARNING, logger="quarry.daemon"),
    ):
        first = asyncio.ensure_future(threads.run(dies))
        while not started.is_set():  # not to_thread: it would start a thread too
            await asyncio.sleep(0.005)
        second = asyncio.ensure_future(threads.run(lambda: "second"))
        third = asyncio.ensure_future(threads.run(lambda: "third"))
        second.add_done_callback(lambda _f: ended.append("second"))
        third.add_done_callback(lambda _f: ended.append("third"))
        await asyncio.sleep(0.05)
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(first, second, third, return_exceptions=True), 5
        )

    for _ in range(200):  # the thread reports after its future has failed
        if died:
            break
        await asyncio.sleep(0.01)
    assert "could not replace an ended thread (no thread for the replacement)" in (
        caplog.text
    )
    assert [args.exc_type for args in died] == [SystemExit]
    assert isinstance(results[0], DaemonThreadAbortedError)
    for stranded in results[1:]:
        assert isinstance(stranded, RuntimeError)
        assert "no thread for the replacement" in str(stranded)
    assert ended == ["second", "third"]  # first in, first failed
    assert await asyncio.wait_for(threads.run(lambda: "later"), 2) == "later"


async def test_a_pool_of_one_runs_in_submission_order() -> None:
    threads = DaemonThreads(max_threads=1)
    order: list[int] = []

    await asyncio.gather(*(threads.run(partial(order.append, n)) for n in range(6)))

    assert order == list(range(6))


def test_a_result_for_a_loop_that_has_closed_is_dropped_without_error() -> None:
    threads = DaemonThreads(max_threads=1)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def work() -> int:
        started.set()
        release.wait(5)
        finished.set()
        return 1

    loop = asyncio.new_event_loop()
    task = loop.create_task(threads.run(work))
    loop.run_until_complete(asyncio.sleep(0.05))
    assert started.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        loop.run_until_complete(task)
    loop.close()  # the loop is gone before the call returns
    release.set()

    assert finished.wait(2)
    assert asyncio.run(threads.run(lambda: 3)) == 3  # the thread went on serving
