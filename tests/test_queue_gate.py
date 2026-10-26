"""QueueGate: depth with a reserve, wake-ups for waiters, and the close signal."""

from __future__ import annotations

import asyncio

from quarry.daemon.queue_bounds import QueueGate
from quarry.stop_signal import StopSignal


def test_slots_are_taken_up_to_the_depth() -> None:
    gate = QueueGate(2)

    assert [gate.try_take(), gate.try_take(), gate.try_take()] == [True, True, False]


def test_a_reserve_keeps_slots_back_for_those_who_ask_for_none() -> None:
    gate = QueueGate(4)

    for _ in range(2):
        assert gate.has_room(reserve=2)
        assert gate.try_take()
    assert not gate.has_room(reserve=2)  # the last two are for hooks
    assert gate.try_take()  # a hook still gets in
    assert gate.try_take()
    assert not gate.try_take()


def test_a_reserve_larger_than_the_queue_still_leaves_one_slot() -> None:
    gate = QueueGate(2)

    assert gate.has_room(reserve=10)
    assert gate.try_take()
    assert not gate.has_room(reserve=10)


def test_a_closed_gate_admits_nothing() -> None:
    gate = QueueGate(4)

    gate.close()

    assert gate.closing
    assert not gate.try_take()


def test_closing_sets_the_stop_signals_registered_for_the_block() -> None:
    gate = QueueGate(1)
    event = StopSignal()

    with gate.stopping(event):
        assert not event.is_set()
        gate.close()
        assert event.is_set()
        assert event.reason == "daemon shutting down"


def test_an_event_registered_after_the_close_is_set_at_once() -> None:
    gate = QueueGate(1)
    gate.close()
    event = StopSignal()

    with gate.stopping(event):
        assert event.is_set()


def test_an_event_registered_for_a_finished_block_is_left_alone() -> None:
    gate = QueueGate(1)
    event = StopSignal()
    with gate.stopping(event):
        pass

    gate.close()

    assert not event.is_set()


async def test_a_waiter_wakes_when_a_slot_is_released() -> None:
    gate = QueueGate(1)
    assert gate.try_take()
    loop = asyncio.get_running_loop()
    started = loop.time()
    waiter = asyncio.create_task(gate.wait_released(30.0))
    await asyncio.sleep(0.05)

    gate.release()
    await waiter

    assert loop.time() - started < 5.0


async def test_a_waiter_gives_up_at_its_timeout() -> None:
    gate = QueueGate(1)
    loop = asyncio.get_running_loop()
    started = loop.time()

    await gate.wait_released(0.1)

    assert 0.05 <= loop.time() - started < 5.0


async def test_a_waiter_wakes_when_the_gate_closes() -> None:
    gate = QueueGate(1)
    waiter = asyncio.create_task(gate.wait_released(30.0))
    await asyncio.sleep(0.05)

    gate.close()

    await asyncio.wait_for(waiter, timeout=5.0)
