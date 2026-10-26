"""CheckRunner: whatever a check raises, it is a result that keeps its weight."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_core import InitErrorDetails

from quarry.doctor_runner import CheckEntry, CheckRunner
from quarry.home_paths import HomeUnresolvedError
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


def _validation_error(*causes: BaseException) -> ValidationError:
    """Build a ValidationError whose errors each carry one of *causes*."""
    return ValidationError.from_exception_data(
        "Settings",
        [
            InitErrorDetails(
                type="value_error", loc=(f"field{n}",), input=None, ctx={"error": cause}
            )
            for n, cause in enumerate(causes)
        ],
    )


def _home() -> HomeUnresolvedError:
    return HomeUnresolvedError("no home")


def test_a_missing_home_is_recognised_by_its_type() -> None:
    assert CheckRunner.is_missing_home(_home())


def test_a_validation_error_of_only_missing_homes_is_one() -> None:
    assert CheckRunner.is_missing_home(_validation_error(_home(), _home()))


def test_a_validation_error_with_any_other_cause_is_not() -> None:
    assert not CheckRunner.is_missing_home(
        _validation_error(_home(), ValueError("bad value"))
    )


def test_a_missing_home_behind_an_explicit_cause_is_found() -> None:
    try:
        raise RuntimeError("could not read") from _home()
    except RuntimeError as wrapped:
        assert CheckRunner.is_missing_home(wrapped)


def test_a_missing_home_that_was_only_in_flight_is_not_the_cause() -> None:
    try:
        try:
            raise _home()
        except HomeUnresolvedError:
            raise RuntimeError("something else") from None
    except RuntimeError as unrelated:
        assert unrelated.__context__ is not None
        assert not CheckRunner.is_missing_home(unrelated)


def test_a_cycle_of_causes_ends() -> None:
    first, second = RuntimeError("a"), RuntimeError("b")
    first.__cause__, second.__cause__ = second, first

    assert not CheckRunner.is_missing_home(first)


def test_a_missing_home_is_a_skipped_line_not_an_issue() -> None:
    def needs_home() -> CheckResult:
        raise _home()

    result = CheckRunner.attempt(CheckEntry("Probe", needs_home, required=True))

    assert result is not None
    assert (result.passed, result.required) == (False, False)
    assert result.message == "skipped (needs the home directory)"
