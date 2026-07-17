"""``TaskOutcome`` and ``QuarryClient.await_task`` behavior.

Covers the regression the typed outcome fixes (``RemoteClient`` printed a hardcoded
``"Deregister failed"`` for every 202 caller) and the bxwd connection-lost
fail-fast (consecutive connection losses short-circuit to ``unreachable`` rather
than polling to the deadline).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Self, final

import pytest

from quarry.client import QuarryClient, TaskOutcome
from quarry.client.errors import QuarryConnectionError
from quarry.client.transport import Response


@final
class ScriptedTransport:
    """A transport that replays a fixed sequence of task-status bodies/errors."""

    __slots__ = ("_steps", "calls")

    _steps: list[object]
    calls: int

    def __new__(cls, steps: list[object]) -> Self:
        self = super().__new__(cls)
        self._steps = steps
        self.calls = 0
        return self

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        timeout: float | None = None,
    ) -> Response:
        step = self._steps[min(self.calls, len(self._steps) - 1)]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        return Response(200, step)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the poll loop spin without real delay."""
    monkeypatch.setattr("quarry.client.client._POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr("quarry.client.client.time.sleep", lambda _s: None)


class TestTaskOutcome:
    def test_completed_carries_results(self) -> None:
        outcome = TaskOutcome.completed("t", {"deleted": 3})
        assert outcome.is_completed
        assert not outcome.is_failed
        assert outcome.result_int("deleted") == 3

    def test_result_int_defaults_zero_for_missing_or_bool(self) -> None:
        outcome = TaskOutcome.completed("t", {"flag": True})
        assert outcome.result_int("flag") == 0
        assert outcome.result_int("absent") == 0

    def test_failed_carries_error(self) -> None:
        outcome = TaskOutcome.failed("t", "boom")
        assert outcome.is_failed
        assert outcome.error == "boom"


class TestAwaitTask:
    def test_completed_returns_results(self) -> None:
        transport = ScriptedTransport(
            [{"task_id": "t", "status": "completed", "results": {"deleted_chunks": 7}}]
        )
        outcome = QuarryClient(transport).await_task("t")
        assert outcome.is_completed
        assert outcome.result_int("deleted_chunks") == 7

    def test_failed_carries_error_not_hardcoded_deregister(self) -> None:
        transport = ScriptedTransport(
            [{"task_id": "t", "status": "failed", "error": "optimize blew up"}]
        )
        outcome = QuarryClient(transport).await_task("t")
        assert outcome.is_failed
        assert outcome.error == "optimize blew up"
        # The regression assertion: the outcome never names an operation.
        assert "Deregister" not in outcome.error

    def test_operation_agnostic_across_callers(self) -> None:
        # The same await_task drives an optimize wait and a deregister wait; each
        # gets its own operation's result dict, no cross-contamination.
        opt_step = {
            "task_id": "t",
            "status": "completed",
            "results": {"optimized": True},
        }
        opt = QuarryClient(ScriptedTransport([opt_step])).await_task("t")
        dereg = QuarryClient(
            ScriptedTransport(
                [
                    {
                        "task_id": "t",
                        "status": "completed",
                        "results": {"deleted_chunks": 4},
                    }
                ]
            )
        ).await_task("t")
        assert opt.results.get("optimized") is True
        assert dereg.result_int("deleted_chunks") == 4

    def test_connection_lost_fail_fast_at_max_polls(self) -> None:
        # bxwd: three consecutive connection losses short-circuit to unreachable
        # at exactly _MAX_UNREACHABLE_POLLS, not after polling to the deadline.
        from quarry.client.client import _MAX_UNREACHABLE_POLLS

        transport = ScriptedTransport(
            [QuarryConnectionError("server gone", "127.0.0.1")]
        )
        outcome = QuarryClient(transport).await_task("t")
        assert outcome.status == "unreachable"
        assert transport.calls == _MAX_UNREACHABLE_POLLS

    def test_single_blip_resets_and_completes(self) -> None:
        # A single transient loss does not fail: the counter resets on the next
        # successful poll.
        transport = ScriptedTransport(
            [
                QuarryConnectionError("blip", "127.0.0.1"),
                {"task_id": "t", "status": "completed", "results": {}},
            ]
        )
        outcome = QuarryClient(transport).await_task("t")
        assert outcome.is_completed
