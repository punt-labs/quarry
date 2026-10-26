"""CheckRunner: whatever a check raises, it is a result that keeps its weight."""

from __future__ import annotations

import pytest

from quarry.doctor_runner import CheckEntry, CheckRunner
from quarry.results import CheckResult


def _boom() -> CheckResult:
    msg = "exploded"
    raise RuntimeError(msg)


@pytest.mark.parametrize("required", [True, False])
def test_a_crashed_check_keeps_the_entrys_required_flag(*, required: bool) -> None:
    result = CheckRunner.attempt(CheckEntry("Probe", _boom, required=required))

    assert result is not None
    assert result.passed is False
    assert result.required is required
    assert "RuntimeError" in result.message


def test_a_diagnostic_with_nothing_to_say_is_left_out_and_the_rest_run_in_order() -> (
    None
):
    entries = [
        CheckEntry("one", lambda: CheckResult(name="one", passed=True, message="")),
        CheckEntry("quiet", lambda: None),
        CheckEntry("crash", _boom),
        CheckEntry("two", lambda: CheckResult(name="two", passed=True, message="")),
    ]

    names = [result.name for result in CheckRunner.run(entries)]

    assert names == ["one", "crash", "two"]
