"""DaemonThreads: blocking work on daemon threads that never hold the process."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from quarry.daemon.daemon_threads import DaemonThreads


async def test_a_call_runs_on_a_daemon_thread_off_the_default_executor() -> None:
    threads = DaemonThreads()

    thread = await threads.run(threading.current_thread)

    assert thread.daemon
    assert thread.name == "quarry-spool"
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
        for _ in range(DaemonThreads.MAX_THREADS + 3)
    ]
    await asyncio.sleep(0.2)
    release.set()
    await asyncio.gather(*pending)

    assert peak == DaemonThreads.MAX_THREADS


async def test_a_result_for_a_closed_loop_is_dropped_without_error() -> None:
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
