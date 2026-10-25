"""ShutdownPolicy: how a teardown budget is shared, and the stop timeout above it."""

from __future__ import annotations

import pytest

from quarry.config import Settings
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
