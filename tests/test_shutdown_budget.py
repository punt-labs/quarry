"""ShutdownBudget: one deadline for the teardown, and a stop timeout above it."""

from __future__ import annotations

import time

from quarry.config import Settings
from quarry.shutdown_budget import ShutdownBudget


def test_the_service_stop_timeout_is_the_total_plus_the_margin() -> None:
    budget = ShutdownBudget(30.0)

    assert budget.stop_timeout_s == 40
    assert budget.stop_timeout_s > budget.total_s


def test_a_fractional_total_rounds_the_stop_timeout_up() -> None:
    assert ShutdownBudget(0.5).stop_timeout_s == 11


def test_the_default_total_is_the_ingest_drain_timeout() -> None:
    assert ShutdownBudget(Settings().ingest_drain_timeout_s).total_s == 30.0


def test_what_remains_shrinks_as_the_teardown_runs_and_never_goes_negative() -> None:
    budget = ShutdownBudget(0.2)
    first = budget.remaining()

    time.sleep(0.3)

    assert 0.0 < first <= 0.2
    assert budget.remaining() == 0.0
