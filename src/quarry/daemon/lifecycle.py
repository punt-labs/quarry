"""The moves a background body makes on its task's status as it ends."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping


class Lifecycle(Protocol):
    """How a task's status moves when its body says, or fails to say, how it ended.

    A queued job and a plain polled task end by the same four moves with
    different rules (a job can be withdrawn and its store can outlive its worker),
    so what records the end takes whichever lifecycle fits the task's kind.
    """

    @property
    def kind(self) -> str:
        """Return the kind of the task this lifecycle moves."""
        ...

    @property
    def task_id(self) -> str:
        """Return the id of the task this lifecycle moves."""
        ...

    def settle_stored(self, results: Mapping[str, object]) -> bool:
        """Complete the task with *results*; return whether it was."""
        ...

    def settle_failed(self, reason: str) -> bool:
        """Fail the task with *reason*; return whether it was."""
        ...

    def cancelled(self, default: str) -> None:
        """Settle a task whose body was cancelled, with *default* as its reason."""
        ...

    def exited(self, default: str) -> None:
        """Fail a task whose body ended with no terminal status, with *default*."""
        ...
