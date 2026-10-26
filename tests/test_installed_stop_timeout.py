"""InstalledStopTimeout: the stop timeout the installed service carries, by policy."""

from __future__ import annotations

import logging
import os
import plistlib
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from quarry.config import Settings
from quarry.daemon.bind_options import BindOptions
from quarry.daemon.launcher import DaemonLauncher
from quarry.doctor_daemon import DaemonDiagnostics
from quarry.installed_budget import BudgetState
from quarry.installed_stop_timeout import InstalledStopTimeout
from quarry.shutdown_policy import ShutdownPolicy
from quarry.stop_timeout_verdict import StopTimeoutState

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_BUDGET = 47.0  # not the default: the verdict must follow the budget it is given
_NEEDED = ShutdownPolicy.stop_timeout_for(_BUDGET)
_STATE = StopTimeoutState


def _plist(path: Path, **keys: object) -> Path:
    path.write_bytes(plistlib.dumps({"Label": "com.punt-labs.quarry", **keys}))
    return path


def _unit(path: Path, *service_lines: str) -> Path:
    body = "\n".join(["[Service]", "Restart=always", *service_lines, ""])
    path.write_text(body)
    return path


def _state(path: Path, total: float = _BUDGET) -> StopTimeoutState:
    return InstalledStopTimeout(path).verdict(total).state


def test_a_plist_without_the_key_is_short_and_names_the_fix(tmp_path: Path) -> None:
    verdict = InstalledStopTimeout(_plist(tmp_path / "q.plist")).verdict(_BUDGET)

    assert verdict.state is _STATE.SHORT
    assert verdict.value is None
    assert "quarry install" in verdict.message
    assert str(tmp_path / "q.plist") in verdict.message


def test_a_unit_without_the_directive_is_short(tmp_path: Path) -> None:
    assert _state(_unit(tmp_path / "q.service")) is _STATE.SHORT


def test_a_stop_timeout_below_the_policy_is_short_with_both_numbers(
    tmp_path: Path,
) -> None:
    below = _plist(tmp_path / "q.plist", ExitTimeOut=_NEEDED - 1)

    verdict = InstalledStopTimeout(below).verdict(_BUDGET)

    assert verdict.state is _STATE.SHORT
    assert str(_NEEDED - 1) in verdict.message
    assert str(_NEEDED) in verdict.message


def test_a_stop_timeout_at_or_above_the_policy_is_ok(tmp_path: Path) -> None:
    at = _plist(tmp_path / "a.plist", ExitTimeOut=_NEEDED)
    above = _unit(tmp_path / "b.service", f"TimeoutStopSec={_NEEDED + 5}")

    assert _state(at) is _STATE.OK
    assert _state(above) is _STATE.OK


def test_the_verdict_follows_the_budget_it_is_given(tmp_path: Path) -> None:
    installed = _plist(tmp_path / "q.plist", ExitTimeOut=_NEEDED)

    assert _state(installed, _BUDGET) is _STATE.OK
    assert _state(installed, _BUDGET * 3) is _STATE.SHORT


def test_nothing_installed_is_its_own_verdict(tmp_path: Path) -> None:
    verdict = InstalledStopTimeout(tmp_path / "absent.plist").verdict(_BUDGET)

    assert verdict.state is _STATE.NOT_INSTALLED
    assert not verdict.is_problem


def test_a_truncated_plist_is_unparseable_and_names_the_class(tmp_path: Path) -> None:
    whole = plistlib.dumps({"Label": "x", "ExitTimeOut": 99})
    truncated = tmp_path / "q.plist"
    truncated.write_bytes(whole[: len(whole) // 2])

    verdict = InstalledStopTimeout(truncated).verdict(_BUDGET)

    assert verdict.state is _STATE.UNPARSEABLE
    assert verdict.is_problem
    assert verdict.detail  # the exception class, never its message


def test_a_plist_whose_root_is_an_array_is_unparseable(tmp_path: Path) -> None:
    array = tmp_path / "q.plist"
    array.write_bytes(plistlib.dumps(["not", "a", "dict"]))

    assert _state(array) is _STATE.UNPARSEABLE


def test_a_directory_at_the_path_is_unreadable_with_its_errno(tmp_path: Path) -> None:
    directory = tmp_path / "q.plist"
    directory.mkdir()

    verdict = InstalledStopTimeout(directory).verdict(_BUDGET)

    assert verdict.state is _STATE.UNREADABLE
    assert verdict.detail == "EISDIR"


def test_a_file_that_cannot_be_opened_is_unreadable(tmp_path: Path) -> None:
    locked = _plist(tmp_path / "q.plist", ExitTimeOut=_NEEDED)
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):  # running as a user who ignores modes
            pytest.skip("file modes are not enforced for this user")
        verdict = InstalledStopTimeout(locked).verdict(_BUDGET)
    finally:
        locked.chmod(0o600)

    assert verdict.state is _STATE.UNREADABLE
    assert verdict.detail == "EACCES"


def test_a_boolean_exit_timeout_is_unrecognized_not_one(tmp_path: Path) -> None:
    flag = _plist(tmp_path / "q.plist", ExitTimeOut=True)

    verdict = InstalledStopTimeout(flag).verdict(_BUDGET)

    assert verdict.state is _STATE.UNRECOGNIZED
    assert verdict.detail == "True"


@pytest.mark.parametrize("value", ["1min", "2min 30s", "90", "90s", "1h"])
def test_systemd_time_spans_are_parsed(tmp_path: Path, value: str) -> None:
    unit = _unit(tmp_path / "q.service", f"TimeoutStopSec={value}")

    verdict = InstalledStopTimeout(unit).verdict(_BUDGET)

    assert verdict.state in {_STATE.OK, _STATE.SHORT}  # read, and judged by value
    assert verdict.value is not None


def test_a_time_span_is_judged_by_its_seconds(tmp_path: Path) -> None:
    long_enough = _unit(tmp_path / "a.service", f"TimeoutStopSec={_NEEDED + 1}s")
    two_terms = _unit(tmp_path / "b.service", "TimeoutStopSec=1min 30s")

    assert _state(long_enough) is _STATE.OK
    assert InstalledStopTimeout(two_terms).verdict(_BUDGET).value == 90.0


def test_infinity_is_never_short(tmp_path: Path) -> None:
    unit = _unit(tmp_path / "q.service", "TimeoutStopSec=infinity")

    verdict = InstalledStopTimeout(unit).verdict(_BUDGET * 100)

    assert verdict.state is _STATE.INFINITE
    assert not verdict.is_problem


@pytest.mark.parametrize("value", ["soon", "12 parsecs", "1,5min", "9" * 30])
def test_a_value_that_cannot_be_interpreted_is_unrecognized(
    tmp_path: Path, value: str
) -> None:
    unit = _unit(tmp_path / "q.service", f"TimeoutStopSec={value}")

    verdict = InstalledStopTimeout(unit).verdict(_BUDGET)

    assert verdict.state is _STATE.UNRECOGNIZED
    assert verdict.detail == value[:40]


def test_the_last_assignment_wins(tmp_path: Path) -> None:
    first_short = _unit(
        tmp_path / "a.service", "TimeoutStopSec=1", f"TimeoutStopSec={_NEEDED}"
    )
    last_short = _unit(
        tmp_path / "b.service", f"TimeoutStopSec={_NEEDED}", "TimeoutStopSec=1"
    )

    assert _state(first_short) is _STATE.OK
    assert _state(last_short) is _STATE.SHORT


def test_an_empty_assignment_resets_to_none(tmp_path: Path) -> None:
    reset = _unit(
        tmp_path / "q.service", f"TimeoutStopSec={_NEEDED}", "TimeoutStopSec="
    )

    assert _state(reset) is _STATE.SHORT


def test_a_commented_directive_does_not_count(tmp_path: Path) -> None:
    unit = _unit(tmp_path / "q.service", f"#TimeoutStopSec={_NEEDED}")

    assert _state(unit) is _STATE.SHORT


_LOW, _HIGH = ShutdownPolicy.total_bounds()


def _with_env(path: Path, **env: object) -> Path:
    return _plist(path, EnvironmentVariables=env)


def test_the_budget_comes_from_the_installed_definition_when_it_sets_one(
    tmp_path: Path,
) -> None:
    plist = _with_env(tmp_path / "q.plist", **{ShutdownPolicy.ENV_NAME: "61"})
    env_file = tmp_path / "quarry.env"
    env_file.write_text(f'MALLOC_CONF="x"\n{ShutdownPolicy(83.0).env_line()}\n')
    unit = _unit(tmp_path / "q.service")

    from_plist = InstalledStopTimeout(plist).budget()
    from_env_file = InstalledStopTimeout(unit, env_file).budget()

    assert (from_plist.state, from_plist.value) == (BudgetState.VALID, 61.0)
    assert (from_env_file.state, from_env_file.value) == (BudgetState.VALID, 83.0)


def test_a_definition_that_sets_no_budget_is_absent_not_invalid(
    tmp_path: Path,
) -> None:
    unit = _unit(tmp_path / "q.service")
    empty_env = tmp_path / "quarry.env"
    empty_env.write_text('MALLOC_CONF="x"\n')

    assert InstalledStopTimeout(unit).budget().state is BudgetState.ABSENT
    assert InstalledStopTimeout(unit, empty_env).budget().state is BudgetState.ABSENT
    assert InstalledStopTimeout(_plist(tmp_path / "q.plist")).budget().state is (
        BudgetState.ABSENT
    )
    assert InstalledStopTimeout(tmp_path / "gone.plist").budget().state is (
        BudgetState.ABSENT
    )


def test_the_last_env_file_assignment_wins(tmp_path: Path) -> None:
    env_file = tmp_path / "quarry.env"
    env_file.write_text(
        f'{ShutdownPolicy.ENV_NAME}="{_LOW}"\n{ShutdownPolicy.ENV_NAME}="{_HIGH}"\n'
    )

    budget = InstalledStopTimeout(_unit(tmp_path / "q.service"), env_file).budget()

    assert budget.value == _HIGH


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("soon", "is not a plain number"),
        ("inf", "is not a plain number"),
        ("nan", "is not a plain number"),
        ("-3", "is not a plain number"),
        ("1e2", "is not a plain number"),
        (" 30", "is not a plain number"),
        ("", "is not a plain number"),
        ("\u0666\u0660\u0660", "is not a plain number"),  # Arabic-Indic 600
        ("\uff13\uff10", "is not a plain number"),  # fullwidth 30
        (str(_LOW - 1), "outside"),
        (str(_HIGH + 1), "outside"),
    ],
)
def test_an_installed_budget_that_cannot_be_used_is_invalid_with_its_reason(
    tmp_path: Path, raw: str, reason: str
) -> None:
    plist = _with_env(tmp_path / "q.plist", **{ShutdownPolicy.ENV_NAME: raw})

    budget = InstalledStopTimeout(plist).budget()

    assert budget.state is BudgetState.INVALID
    assert reason in budget.reason
    assert "quarry install" in budget.message
    assert str(plist) in budget.message


def test_the_bounds_themselves_are_valid_installed_budgets(tmp_path: Path) -> None:
    for bound in (_LOW, _HIGH):
        plist = _with_env(tmp_path / "q.plist", **{ShutdownPolicy.ENV_NAME: str(bound)})

        assert InstalledStopTimeout(plist).budget().value == bound


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({ShutdownPolicy.ENV_NAME: {"a": 1}}, "dict, not a string"),
        ({ShutdownPolicy.ENV_NAME: 30}, "int, not a string"),
        ({ShutdownPolicy.ENV_NAME: [1]}, "list, not a string"),
    ],
)
def test_an_installed_budget_that_is_not_a_string_is_invalid(
    tmp_path: Path, env: dict[str, object], reason: str
) -> None:
    plist = _with_env(tmp_path / "q.plist", **env)

    budget = InstalledStopTimeout(plist).budget()

    assert budget.state is BudgetState.INVALID
    assert reason in budget.reason


def test_a_plist_whose_environment_is_not_a_dictionary_is_invalid(
    tmp_path: Path,
) -> None:
    plist = _plist(tmp_path / "q.plist", EnvironmentVariables=["x"])

    budget = InstalledStopTimeout(plist).budget()

    assert budget.state is BudgetState.INVALID
    assert "EnvironmentVariables is not a dictionary" in budget.reason


def test_an_array_rooted_or_broken_plist_gives_an_invalid_budget(
    tmp_path: Path,
) -> None:
    array = tmp_path / "a.plist"
    array.write_bytes(plistlib.dumps(["x"]))
    broken = tmp_path / "b.plist"
    broken.write_bytes(b"<plist><dict>")

    assert "root is not a dictionary" in InstalledStopTimeout(array).budget().reason
    assert "cannot be parsed" in InstalledStopTimeout(broken).budget().reason


def test_a_directory_where_the_env_definition_should_be_is_unreadable(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "q.plist"
    directory.mkdir()

    budget = InstalledStopTimeout(directory).budget()

    assert budget.state is BudgetState.INVALID
    assert "EISDIR" in budget.reason


def test_a_bare_zero_timeout_is_infinite_as_systemd_treats_it(tmp_path: Path) -> None:
    unit = _unit(tmp_path / "q.service", "TimeoutStopSec=0")

    assert _state(unit, _BUDGET * 100) is _STATE.INFINITE


def test_a_unit_that_sets_only_timeout_sec_uses_it(tmp_path: Path) -> None:
    long_enough = _unit(tmp_path / "a.service", f"TimeoutSec={_NEEDED}")
    too_short = _unit(tmp_path / "b.service", "TimeoutSec=1")

    assert _state(long_enough) is _STATE.OK
    assert _state(too_short) is _STATE.SHORT


def test_the_later_of_timeout_sec_and_timeout_stop_sec_wins(tmp_path: Path) -> None:
    stop_last = _unit(
        tmp_path / "a.service", "TimeoutSec=1", f"TimeoutStopSec={_NEEDED}"
    )
    sec_last = _unit(
        tmp_path / "b.service", f"TimeoutStopSec={_NEEDED}", "TimeoutSec=1"
    )

    assert _state(stop_last) is _STATE.OK
    assert _state(sec_last) is _STATE.SHORT


def test_a_hostile_time_span_is_read_in_linear_time(tmp_path: Path) -> None:
    terms = "1s " * 5000
    hostile = [
        _unit(tmp_path / "a.service", f"TimeoutStopSec={terms}junk"),
        _unit(tmp_path / "b.service", f"TimeoutStopSec={'9' * 100_000}"),
        _unit(tmp_path / "c.service", f"TimeoutStopSec={' ' * 50_000}x"),
        _unit(tmp_path / "d.service", f"TimeoutStopSec={terms}"),
    ]

    started = time.process_time()
    states = [_state(path) for path in hostile]
    elapsed = time.process_time() - started

    assert states[:3] == [_STATE.UNRECOGNIZED] * 3
    assert states[3] is _STATE.OK  # 5000 seconds of terms, and they all parse
    assert elapsed < 2.0  # the bound; a backtracking regex takes minutes


def test_report_logs_a_short_stop_timeout_at_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    short = _plist(tmp_path / "q.plist")

    with caplog.at_level(logging.WARNING, logger="quarry.installed_stop_timeout"):
        InstalledStopTimeout(short).report(_BUDGET)

    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert "quarry install" in record.getMessage()


def test_report_logs_any_other_problem_at_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    broken = tmp_path / "q.plist"
    broken.write_text("not a plist")

    with caplog.at_level(logging.WARNING, logger="quarry.installed_stop_timeout"):
        InstalledStopTimeout(broken).report(_BUDGET)

    [record] = caplog.records
    assert record.levelno == logging.WARNING


def test_report_logs_an_info_line_when_nothing_is_wrong(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    good = _plist(tmp_path / "good.plist", ExitTimeOut=_NEEDED)
    forever = _unit(tmp_path / "forever.service", "TimeoutStopSec=infinity")
    absent = tmp_path / "absent.plist"

    for path in (good, forever, absent):
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="quarry.installed_stop_timeout"):
            InstalledStopTimeout(path).report(_BUDGET)

        [record] = caplog.records
        assert record.levelno == logging.INFO


def test_report_never_raises_even_when_the_check_itself_breaks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reader = InstalledStopTimeout(tmp_path / "q.plist")

    with (
        patch.object(InstalledStopTimeout, "verdict", side_effect=RuntimeError("boom")),
        caplog.at_level(logging.ERROR, logger="quarry.installed_stop_timeout"),
    ):
        reader.report(_BUDGET)

    assert "could not be checked" in caplog.text
    assert "boom" not in caplog.text  # class and frames only


def test_doctor_flags_an_old_installed_service_and_says_where_the_budget_is_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _plist(tmp_path / "quarry.plist")
    monkeypatch.setenv(ShutdownPolicy.ENV_NAME, f"{_BUDGET:g}")

    with patch.object(
        InstalledStopTimeout, "installed", return_value=InstalledStopTimeout(old)
    ):
        check = DaemonDiagnostics.stop_timeout()

    assert not check.passed
    assert "quarry install" in check.message
    assert str(_NEEDED) in check.message  # from the env override
    assert "from settings" in check.message


def test_doctor_prefers_the_installed_definitions_own_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ShutdownPolicy.ENV_NAME, "30")
    installed_total = 120.0
    plist = _plist(
        tmp_path / "quarry.plist",
        ExitTimeOut=ShutdownPolicy.stop_timeout_for(installed_total),
        EnvironmentVariables={ShutdownPolicy.ENV_NAME: f"{installed_total:g}"},
    )

    with patch.object(
        InstalledStopTimeout, "installed", return_value=InstalledStopTimeout(plist)
    ):
        check = DaemonDiagnostics.stop_timeout()

    assert check.passed
    assert "the installed service definition" in check.message


def test_doctor_passes_a_current_installed_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = _plist(tmp_path / "quarry.plist", ExitTimeOut=_NEEDED)
    monkeypatch.setenv(ShutdownPolicy.ENV_NAME, f"{_BUDGET:g}")

    with patch.object(
        InstalledStopTimeout, "installed", return_value=InstalledStopTimeout(current)
    ):
        check = DaemonDiagnostics.stop_timeout()

    assert check.passed


def test_doctor_passes_when_no_service_is_installed(tmp_path: Path) -> None:
    absent = InstalledStopTimeout(tmp_path / "none.plist")

    with patch.object(InstalledStopTimeout, "installed", return_value=absent):
        check = DaemonDiagnostics.stop_timeout()

    assert check.passed
    assert check.message == "no installed service; not checked"


@pytest.mark.parametrize("corrupt", [b"\x00\x01 not xml", b"", b"<plist><dict>"])
def test_doctor_reports_a_corrupt_plist_instead_of_raising(
    tmp_path: Path, corrupt: bytes
) -> None:
    broken = tmp_path / "quarry.plist"
    broken.write_bytes(corrupt)

    with patch.object(
        InstalledStopTimeout, "installed", return_value=InstalledStopTimeout(broken)
    ):
        check = DaemonDiagnostics.stop_timeout()

    assert not check.passed


def test_doctor_reports_a_failing_check_instead_of_raising() -> None:
    with patch.object(InstalledStopTimeout, "installed", side_effect=OSError("boom")):
        check = DaemonDiagnostics.stop_timeout()

    assert not check.passed
    assert "OSError" in check.message


def _options() -> BindOptions:
    return BindOptions(
        host="127.0.0.1", port=8420, db="", api_key=None, cors_origins=(), tls=False
    )


@pytest.mark.parametrize("corrupt", [b"\x00\x01 not xml", b"<plist><dict>", b"[]"])
def test_the_daemon_still_starts_with_a_corrupt_installed_plist(
    tmp_path: Path, corrupt: bytes
) -> None:
    broken = tmp_path / "quarry.plist"
    broken.write_bytes(corrupt)

    with (
        patch("quarry.daemon.launcher.DaemonServer.serve") as serve,
        patch.object(
            InstalledStopTimeout, "installed", return_value=InstalledStopTimeout(broken)
        ),
    ):
        DaemonLauncher(_options()).launch()

    serve.assert_called_once()


def test_the_daemon_still_starts_when_the_check_itself_blows_up() -> None:
    with (
        patch("quarry.daemon.launcher.DaemonServer.serve") as serve,
        patch.object(InstalledStopTimeout, "verdict", side_effect=RuntimeError("boom")),
    ):
        DaemonLauncher(_options()).launch()

    serve.assert_called_once()


def test_the_daemon_logs_an_error_at_start_for_an_old_installed_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    old = _plist(tmp_path / "quarry.plist")
    monkeypatch.setenv(ShutdownPolicy.ENV_NAME, f"{_BUDGET:g}")

    with (
        patch("quarry.daemon.launcher.DaemonServer.serve"),
        patch.object(
            InstalledStopTimeout, "installed", return_value=InstalledStopTimeout(old)
        ),
        caplog.at_level(logging.ERROR, logger="quarry.installed_stop_timeout"),
    ):
        DaemonLauncher(_options()).launch()

    assert "quarry install" in caplog.text
    assert str(_NEEDED) in caplog.text  # the env override flowed in


@pytest.mark.parametrize(
    ("system", "name"), [("Darwin", ".plist"), ("Linux", ".service")]
)
def test_each_platform_reads_its_own_service_definition(system: str, name: str) -> None:
    with patch("quarry.installed_stop_timeout.platform.system", return_value=system):
        reader = InstalledStopTimeout.installed()

    path = reader.verdict(_BUDGET).path  # the file the reader looks at
    assert path.suffix == name
    assert path.name.startswith(
        "com.punt-labs.quarry" if system == "Darwin" else "quarry"
    )


def test_doctor_names_an_invalid_installed_budget_instead_of_saying_settings(
    tmp_path: Path,
) -> None:
    bad = _with_env(
        tmp_path / "quarry.plist", **{ShutdownPolicy.ENV_NAME: str(_HIGH + 1)}
    )

    with patch.object(
        InstalledStopTimeout, "installed", return_value=InstalledStopTimeout(bad)
    ):
        check = DaemonDiagnostics.stop_timeout()

    assert not check.passed
    assert "outside" in check.message
    assert str(bad) in check.message
    assert "quarry install" in check.message
    assert "from settings" not in check.message


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda p: p.write_bytes(plistlib.dumps([])), "root is not a dictionary"),
        (
            lambda p: p.write_bytes(
                plistlib.dumps(
                    {"EnvironmentVariables": {ShutdownPolicy.ENV_NAME: {"x": 1}}}
                )
            ),
            "dict, not a string",
        ),
    ],
)
def test_doctor_reports_the_reason_for_an_unusable_installed_definition(
    tmp_path: Path, make: Callable[[Path], object], reason: str
) -> None:
    path = tmp_path / "quarry.plist"
    make(path)

    with patch.object(
        InstalledStopTimeout, "installed", return_value=InstalledStopTimeout(path)
    ):
        check = DaemonDiagnostics.stop_timeout()

    assert not check.passed
    assert reason in check.message


def test_doctor_says_from_settings_only_when_the_installed_budget_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ShutdownPolicy.ENV_NAME, f"{_BUDGET:g}")
    plist = _plist(tmp_path / "quarry.plist", ExitTimeOut=_NEEDED)

    with patch.object(
        InstalledStopTimeout, "installed", return_value=InstalledStopTimeout(plist)
    ):
        check = DaemonDiagnostics.stop_timeout()

    assert check.passed
    assert f"budget {_BUDGET:g}s from settings" in check.message


def test_doctor_names_the_settings_when_they_are_what_is_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist = _plist(tmp_path / "quarry.plist", ExitTimeOut=_NEEDED)

    with (
        monkeypatch.context() as scoped,
        patch.object(
            InstalledStopTimeout, "installed", return_value=InstalledStopTimeout(plist)
        ),
    ):
        scoped.setenv(ShutdownPolicy.ENV_NAME, "nonsense")
        check = DaemonDiagnostics.stop_timeout()

    assert not check.passed
    assert "settings are invalid" in check.message
    assert "ingest_drain_timeout_s" in check.message
    assert "installed service" not in check.message


def test_a_unit_is_read_the_way_systemd_reads_it(tmp_path: Path) -> None:
    ok = f"TimeoutStopSec={_NEEDED}"
    sections = _unit(tmp_path / "a.service", "TimeoutStopSec=1")
    sections.write_text(
        f"[Unit]\nTimeoutStopSec={_NEEDED}\n[Service]\nTimeoutStopSec=1\n"
        f"[Install]\n{ok}\n"
    )

    assert _state(sections) is _STATE.SHORT  # only [Service] counts


def test_a_directive_outside_the_service_section_does_not_count(
    tmp_path: Path,
) -> None:
    unit = tmp_path / "q.service"
    unit.write_text(f"[Unit]\nTimeoutStopSec={_NEEDED}\n[Service]\nRestart=always\n")

    assert _state(unit) is _STATE.SHORT


def test_a_backslash_continuation_is_joined(tmp_path: Path) -> None:
    unit = tmp_path / "q.service"
    unit.write_text("[Service]\nTimeoutStopSec=1min \\\n30s\n")

    assert InstalledStopTimeout(unit).verdict(_BUDGET).value == 90.0


@pytest.mark.parametrize("comment", ["#", ";", "  #", "\t;"])
def test_comment_lines_are_skipped(tmp_path: Path, comment: str) -> None:
    unit = tmp_path / "q.service"
    unit.write_text(f"[Service]\n{comment}TimeoutStopSec=1\nTimeoutStopSec={_NEEDED}\n")

    assert _state(unit) is _STATE.OK


def test_only_a_newline_ends_a_line(tmp_path: Path) -> None:
    unit = tmp_path / "q.service"
    # A form feed or a lone carriage return is not a line break to systemd, so
    # the second "assignment" is part of the first line's value.
    unit.write_text(f"[Service]\nTimeoutStopSec={_NEEDED}\x0cTimeoutStopSec=1\n")

    assert _state(unit) is _STATE.UNRECOGNIZED


def test_an_env_file_that_cannot_be_decoded_is_invalid_with_that_reason(
    tmp_path: Path,
) -> None:
    unit = _unit(tmp_path / "q.service")
    env_file = tmp_path / "quarry.env"
    env_file.write_bytes(b"INGEST_DRAIN_TIMEOUT_S=\xff\xfe\n")

    budget = InstalledStopTimeout(unit, env_file).budget()

    assert budget.state is BudgetState.INVALID
    assert budget.reason == "cannot be decoded"
    assert budget.path == env_file  # the file actually read, not the unit


def test_an_invalid_plist_budget_names_the_plist(tmp_path: Path) -> None:
    plist = _with_env(tmp_path / "q.plist", **{ShutdownPolicy.ENV_NAME: "soon"})

    assert InstalledStopTimeout(plist).budget().path == plist


def test_a_stale_env_file_with_no_unit_installed_passes_as_not_installed(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "quarry.env"
    env_file.write_text(f'{ShutdownPolicy.ENV_NAME}="soon"\n')  # unusable, but stale
    absent = InstalledStopTimeout(tmp_path / "gone.service", env_file)

    with patch.object(InstalledStopTimeout, "installed", return_value=absent):
        check = DaemonDiagnostics.stop_timeout()

    assert check.passed
    assert check.message == "no installed service; not checked"


def test_doctor_and_the_daemon_agree_that_non_ascii_digits_are_not_a_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = "\u0666\u0660\u0660"
    plist = _with_env(tmp_path / "q.plist", **{ShutdownPolicy.ENV_NAME: raw})

    with monkeypatch.context() as patched:
        patched.setenv(ShutdownPolicy.ENV_NAME, raw)
        with pytest.raises(ValueError, match="valid number"):
            Settings()

    assert InstalledStopTimeout(plist).budget().state is BudgetState.INVALID
