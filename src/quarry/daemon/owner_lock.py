"""A lock that knows which thread holds it."""

from __future__ import annotations

import threading
from typing import Protocol, Self, final


class ClaimNotHeldError(RuntimeError):
    """A task's state was about to be written by a thread without its claim."""


class Claim(Protocol):
    """What a job's status writers need of its lock: take it, and ask who has it."""

    def __enter__(self) -> object:
        """Take the claim."""
        ...

    def __exit__(self, *exc: object) -> None:
        """Release the claim."""
        ...

    def owned(self) -> bool:
        """Return whether the calling thread holds the claim."""
        ...


@final
class OwnerLock:
    """A mutual-exclusion lock that records its holder.

    A plain lock says only that *somebody* holds it, so a write that forgot the
    lock passes a check made by the thread that did not.  This one answers for the
    calling thread, so a status write can assert that it holds the claim itself.
    It is not reentrant: a move that takes the claim never calls another that
    does.
    """

    __slots__ = ("_lock", "_owner")

    _lock: threading.Lock
    _owner: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._lock = threading.Lock()
        self._owner = 0
        return self

    def __enter__(self) -> Self:
        self._lock.acquire()
        self._owner = threading.get_ident()
        return self

    def __exit__(self, *exc: object) -> None:
        self._owner = 0
        self._lock.release()

    def owned(self) -> bool:
        """Return whether the calling thread holds the lock."""
        return self._owner == threading.get_ident()
