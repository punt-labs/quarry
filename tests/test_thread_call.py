"""ThreadCall: a thread's wait on the loop ends with the answer, or says why not."""

from __future__ import annotations

import asyncio
import time

import pytest

from quarry.daemon.thread_call import ThreadCall, ThreadCallAbortedError
from quarry.stop_signal import StopSignal


async def test_the_result_of_the_coroutine_is_returned() -> None:
    async def answer() -> int:
        return 7

    call = ThreadCall.here(StopSignal(), 5.0)

    assert await asyncio.to_thread(call.result, answer()) == 7


async def test_a_timeout_the_coroutine_raised_propagates_at_once() -> None:
    async def times_out() -> None:
        msg = "the coroutine's own timeout"
        raise TimeoutError(msg)

    call = ThreadCall.here(StopSignal(), 30.0)
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="the coroutine's own"):
        await asyncio.to_thread(call.result, times_out())

    assert time.monotonic() - started < 0.4  # not a poll, and not the 30s wait


async def test_a_stop_ends_the_wait_with_its_reason() -> None:
    async def forever() -> None:
        await asyncio.sleep(60)

    stop = StopSignal()
    call = ThreadCall.here(stop, 30.0)
    asyncio.get_running_loop().call_later(0.1, stop.set, "task cancelled")

    with pytest.raises(ThreadCallAbortedError, match="task cancelled"):
        await asyncio.to_thread(call.result, forever())


async def test_a_wait_that_runs_out_says_the_loop_is_not_responding() -> None:
    async def forever() -> None:
        await asyncio.sleep(60)

    call = ThreadCall.here(StopSignal(), 0.2)

    with pytest.raises(ThreadCallAbortedError, match="not responding"):
        await asyncio.to_thread(call.result, forever())
