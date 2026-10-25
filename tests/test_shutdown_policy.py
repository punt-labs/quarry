"""ShutdownPolicy: how a teardown budget is shared, and the stop timeout above it."""

from __future__ import annotations

import pytest

from quarry.config import Settings
from quarry.daemon.ingest_queue import IngestQueue
from quarry.daemon.task_drain import TaskDrain
from quarry.shutdown_policy import ShutdownPolicy

_BUDGETS = [0.0, 0.5, 7.0, 30.0, 123.4]


@pytest.mark.parametrize("total", _BUDGETS)
def test_the_stop_timeout_is_a_whole_number_of_seconds_above_the_total(
    total: float,
) -> None:
    timeout = ShutdownPolicy.stop_timeout_for(total)

    assert isinstance(timeout, int)
    assert timeout > total
    assert timeout - total <= ShutdownPolicy.MARGIN_S + 1  # the margin, rounded up


def test_a_larger_budget_never_has_a_smaller_stop_timeout() -> None:
    timeouts = [ShutdownPolicy.stop_timeout_for(total) for total in sorted(_BUDGETS)]

    assert timeouts == sorted(timeouts)


@pytest.mark.parametrize("total", _BUDGETS)
def test_the_instance_agrees_with_the_pure_function(total: float) -> None:
    policy = ShutdownPolicy(total)

    assert policy.stop_timeout_s == ShutdownPolicy.stop_timeout_for(total)
    assert policy.total_s == total


@pytest.mark.parametrize("total", [7.0, 30.0, 123.4])
def test_the_steps_fit_inside_the_budget(total: float) -> None:
    policy = ShutdownPolicy(total)

    assert policy.watch_s + policy.queue_min_s <= policy.total_s
    assert policy.scan_bound_s <= ShutdownPolicy.SCAN_CAP_S
    assert policy.scan_bound_s <= policy.total_s


def test_the_scan_bound_gives_up_before_the_budget_is_spent() -> None:
    policy = ShutdownPolicy(30.0)

    assert policy.scan_bound_s < policy.total_s - policy.watch_s


def test_the_env_line_carries_the_total_the_daemon_reads_back() -> None:
    line = ShutdownPolicy(75.0).env_line()

    name, _, value = line.partition("=")
    assert name == ShutdownPolicy.ENV_NAME
    assert float(value.strip('"')) == 75.0


def test_the_env_name_is_a_settings_field() -> None:
    assert ShutdownPolicy.ENV_NAME.lower() in Settings.model_fields


def test_from_settings_follows_the_configured_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ShutdownPolicy.ENV_NAME, "55")

    assert ShutdownPolicy.from_settings().total_s == 55.0


def test_the_stop_margin_pays_for_everything_outside_the_budget() -> None:
    outside = (
        ShutdownPolicy.GRACEFUL_S
        + ShutdownPolicy.ABORT_WAIT_S
        + ShutdownPolicy.TASK_FLOOR_S
        + ShutdownPolicy.EXIT_SLACK_S
    )

    assert outside <= ShutdownPolicy.MARGIN_S
    assert ShutdownPolicy.GRACEFUL_S > 0
    assert ShutdownPolicy.GRACEFUL_S < ShutdownPolicy.MARGIN_S - (
        ShutdownPolicy.ABORT_WAIT_S
    )


@pytest.mark.parametrize("timeout", [0.0, 1.0, 17.0])
def test_the_cancel_share_is_part_of_the_timeout(timeout: float) -> None:
    cancel = ShutdownPolicy.cancel_s(timeout)

    assert 0.0 <= cancel <= timeout
    assert cancel == timeout * ShutdownPolicy.CANCEL_SHARE


def test_the_collaborators_use_the_policys_constants() -> None:
    assert IngestQueue.ABORT_WAIT_S == ShutdownPolicy.ABORT_WAIT_S
    assert TaskDrain.FLOOR_S == ShutdownPolicy.TASK_FLOOR_S


@pytest.mark.parametrize("total", ShutdownPolicy.total_bounds())
def test_the_worst_case_is_strictly_below_the_stop_timeout_at_both_bounds(
    total: float,
) -> None:
    worst = (
        ShutdownPolicy.GRACEFUL_S
        + total
        + ShutdownPolicy.ABORT_WAIT_S
        + ShutdownPolicy.TASK_FLOOR_S
        + ShutdownPolicy.EXIT_SLACK_S
    )

    assert worst < ShutdownPolicy.stop_timeout_for(total)
    assert ShutdownPolicy.stop_timeout_for(total) - worst >= ShutdownPolicy.HEADROOM_S


def test_the_bounds_are_the_settings_own() -> None:
    low, high = ShutdownPolicy.total_bounds()

    assert 0 < low < high
    assert Settings(ingest_drain_timeout_s=low).ingest_drain_timeout_s == low
    assert Settings(ingest_drain_timeout_s=high).ingest_drain_timeout_s == high
