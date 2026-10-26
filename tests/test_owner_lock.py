"""OwnerLock: a mutual-exclusion lock that answers for the calling thread."""

from __future__ import annotations

import threading

from quarry.daemon.owner_lock import OwnerLock


def test_the_holder_owns_the_lock_and_nobody_else_does() -> None:
    lock = OwnerLock()
    seen: list[bool] = []

    with lock:
        assert lock.owned()
        thread = threading.Thread(target=lambda: seen.append(lock.owned()))
        thread.start()
        thread.join()

    assert seen == [False]
    assert not lock.owned()


def test_a_second_thread_waits_for_the_holder() -> None:
    lock = OwnerLock()
    order: list[str] = []
    entered = threading.Event()

    def other() -> None:
        entered.set()
        with lock:
            order.append("other")

    with lock:
        thread = threading.Thread(target=other)
        thread.start()
        entered.wait(5)
        order.append("holder")
    thread.join()

    assert order == ["holder", "other"]
