"""Tests for ScanSummary: tracking child scans and settling the umbrella task."""

from __future__ import annotations

import pytest

from quarry.daemon.scan_summary import ScanSummary
from quarry.daemon.task_state import TaskState


def _child(
    status: str = "completed", results: dict[str, object] | None = None
) -> TaskState:
    """Return a task in *status* carrying *results*."""
    state = TaskState("sync-x", "sync")
    if status == "completed":
        state.complete(results or {})
    elif status == "failed":
        state.fail("boom")
        state.record(results or {})
    return state


def _umbrella() -> TaskState:
    return TaskState("sync-umbrella", "sync")


def test_pending_is_true_while_any_child_is_running() -> None:
    assert ScanSummary([_child(), _child("running")], 1).pending() is True


def test_pending_is_false_when_every_child_is_terminal() -> None:
    assert ScanSummary([_child(), _child("failed")], 1).pending() is False


def test_pending_is_false_for_no_children() -> None:
    assert ScanSummary([], 0).pending() is False


def test_settle_completes_the_umbrella_when_clean() -> None:
    umbrella = _umbrella()
    ScanSummary([_child(results={"failed": 0})], 2).settle(umbrella, timed_out=False)
    assert umbrella.status == "completed"
    assert umbrella.results == {
        "collections": 2,
        "failed": 0,
        "shed": 0,
        "errors": [],
    }


def test_settle_with_no_children_completes_with_zero_collections() -> None:
    umbrella = _umbrella()
    ScanSummary([], 0).settle(umbrella, timed_out=False)
    assert umbrella.status == "completed"
    assert umbrella.results["collections"] == 0


def test_settle_fails_when_a_child_was_shed() -> None:
    umbrella = _umbrella()
    ScanSummary([_child("failed"), _child()], 1).settle(umbrella, timed_out=False)
    assert umbrella.status == "failed"
    assert umbrella.results["shed"] == 1
    assert "1 scan job(s) shed, 0 file(s) failed" in umbrella.error


def test_settle_fails_on_per_file_failures_and_keeps_their_errors() -> None:
    umbrella = _umbrella()
    children = [
        _child(results={"failed": 2, "errors": ["a.md: bad", "b.md: bad"]}),
        _child(results={"failed": 1, "errors": ["c.md: bad"]}),
    ]
    ScanSummary(children, 2).settle(umbrella, timed_out=False)
    assert umbrella.status == "failed"
    assert umbrella.results["failed"] == 3
    assert umbrella.results["errors"] == ["a.md: bad", "b.md: bad", "c.md: bad"]
    assert "0 scan job(s) shed, 3 file(s) failed" in umbrella.error


def test_settle_fails_on_timeout_even_when_children_are_clean() -> None:
    umbrella = _umbrella()
    ScanSummary([_child()], 1).settle(umbrella, timed_out=True)
    assert umbrella.status == "failed"
    assert "timed out" in umbrella.error
    assert umbrella.results["collections"] == 1


@pytest.mark.parametrize(
    "results",
    [
        {"failed": "3", "errors": "oops"},
        {"failed": None, "errors": {"a": 1}},
        {"failed": [1], "errors": 7},
    ],
)
def test_settle_ignores_results_of_the_wrong_type(results: dict[str, object]) -> None:
    umbrella = _umbrella()
    ScanSummary([_child(results=results)], 1).settle(umbrella, timed_out=False)
    assert umbrella.status == "completed"
    assert umbrella.results["failed"] == 0
    assert umbrella.results["errors"] == []
