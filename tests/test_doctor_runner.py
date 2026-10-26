"""CheckRunner: whatever a check raises, it is a result that keeps its weight."""

from __future__ import annotations

import pytest

from quarry.config import Settings
from quarry.doctor_preconditions import Need, Preconditions
from quarry.doctor_runner import CheckEntry, CheckRunner
from quarry.home_paths import HomeUnresolvedError
from quarry.results import CheckResult


def _resolved() -> Preconditions:
    return Preconditions(None, Settings(), None)


def _no_home(*, invalid: Exception | None = None) -> Preconditions:
    return Preconditions(HomeUnresolvedError("no home"), None, invalid)


def _ran() -> CheckResult:
    return CheckResult(name="probe", passed=True, message="ran")


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

    names = [result.name for result in CheckRunner.run(entries, _resolved())]

    assert names == ["one", "crash", "two"]


def test_an_entry_whose_home_is_missing_is_skipped_never_dropped() -> None:
    entry = CheckEntry("Probe", _ran, required=True, needs=Need.HOME)

    [result] = CheckRunner.run([entry], _no_home())

    assert (result.name, result.passed, result.required) == ("Probe", False, False)
    assert result.message == "skipped (needs the home directory)"


def test_an_entry_whose_settings_are_invalid_is_skipped_for_valid_settings() -> None:
    entry = CheckEntry("Probe", _ran, needs=Need.HOME | Need.SETTINGS)
    pre = Preconditions(None, None, ValueError("bad"))

    [result] = CheckRunner.run([entry], pre)

    assert result.message == "skipped (needs valid settings)"


def test_invalid_settings_are_named_before_a_missing_home() -> None:
    entry = CheckEntry("Probe", _ran, needs=Need.HOME | Need.SETTINGS)

    [result] = CheckRunner.run([entry], _no_home(invalid=ValueError("bad")))

    assert result.message == "skipped (needs valid settings)"


def test_the_home_is_named_when_it_alone_blocks_the_settings() -> None:
    entry = CheckEntry("Probe", _ran, needs=Need.HOME | Need.SETTINGS)

    [result] = CheckRunner.run([entry], _no_home())

    assert result.message == "skipped (needs the home directory)"


def test_settings_that_only_the_home_blocks_are_skipped_for_the_home() -> None:
    entry = CheckEntry("Probe", _ran, needs=Need.SETTINGS)

    [result] = CheckRunner.run([entry], _no_home())

    assert result.message == "skipped (needs the home directory)"


def test_an_entry_that_needs_nothing_runs_whatever_failed() -> None:
    [result] = CheckRunner.run([CheckEntry("Probe", _ran)], _no_home())

    assert result.message == "ran"


def test_the_home_and_settings_checks_are_where_the_faults_are_counted() -> None:
    pre = _no_home(invalid=ValueError("bad"))

    home, settings = pre.home_check(), pre.settings_check()

    assert (home.passed, home.required) == (False, True)
    assert (settings.passed, settings.required) == (False, True)
    assert "invalid settings" in settings.message


def test_settings_only_the_home_blocks_are_a_skipped_line_not_a_second_issue() -> None:
    settings = _no_home().settings_check()

    assert (settings.passed, settings.required) == (False, False)
    assert settings.message == "skipped (needs the home directory)"


def test_resolved_preconditions_hold_the_settings_for_the_checks() -> None:
    pre = _resolved()

    assert pre.missing(Need.HOME | Need.SETTINGS) is None
    assert pre.settings.lancedb_path.name == "lancedb"
