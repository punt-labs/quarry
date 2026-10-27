"""ShutdownBudget: the clock of one teardown."""

from __future__ import annotations

import time

from quarry.shutdown_budget import ShutdownBudget


def test_what_remains_shrinks_as_the_teardown_runs_and_never_goes_negative() -> None:
    budget = ShutdownBudget(0.2)
    first = budget.remaining()

    time.sleep(0.3)

    assert 0.0 < first <= 0.2
    assert budget.remaining() == 0.0
