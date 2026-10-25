"""InstalledStopTimeout: the stop timeout the installed service carries, by policy."""

from __future__ import annotations

import logging
import plistlib
from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.daemon.bind_options import BindOptions
from quarry.daemon.launcher import DaemonLauncher
from quarry.doctor_daemon import DaemonDiagnostics
from quarry.installed_stop_timeout import InstalledStopTimeout
from quarry.shutdown_policy import ShutdownPolicy

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_BUDGET = 47.0  # not the default: the verdict must follow the policy it is given
_POLICY = ShutdownPolicy(_BUDGET)


def _plist(path: Path, **keys: object) -> Path:
    path.write_bytes(plistlib.dumps({"Label": "com.punt-labs.quarry", **keys}))
    return path


def _unit(path: Path, *service_lines: str) -> Path:
    body = "\n".join(["[Service]", "Restart=always", *service_lines, ""])
    path.write_text(body)
    return path


def test_a_plist_without_the_key_is_flagged_and_names_the_fix(tmp_path: Path) -> None:
    old = _plist(tmp_path / "quarry.plist")

    problem = InstalledStopTimeout(old).problem(_BUDGET)

    assert "has none" in problem
    assert "quarry install" in problem


def test_a_unit_without_the_directive_is_flagged_and_names_the_fix(
    tmp_path: Path,
) -> None:
    old = _unit(tmp_path / "quarry.service")

    problem = InstalledStopTimeout(old).problem(_BUDGET)

    assert "has none" in problem
    assert "quarry install" in problem


def test_a_stop_timeout_below_the_policy_is_flagged_with_both_numbers(
    tmp_path: Path,
) -> None:
    below = _plist(tmp_path / "quarry.plist", ExitTimeOut=_POLICY.stop_timeout_s - 1)

    problem = InstalledStopTimeout(below).problem(_BUDGET)

    assert str(_POLICY.stop_timeout_s - 1) in problem
    assert str(_POLICY.stop_timeout_s) in problem


def test_a_stop_timeout_at_or_above_the_policy_is_fine(tmp_path: Path) -> None:
    at = _plist(tmp_path / "a.plist", ExitTimeOut=_POLICY.stop_timeout_s)
    above = _unit(
        tmp_path / "b.service", f"TimeoutStopSec={_POLICY.stop_timeout_s + 5}"
    )

    assert InstalledStopTimeout(at).problem(_BUDGET) == ""
    assert InstalledStopTimeout(above).problem(_BUDGET) == ""


def test_the_verdict_follows_the_budget_it_is_given(tmp_path: Path) -> None:
    installed = _plist(tmp_path / "q.plist", ExitTimeOut=_POLICY.stop_timeout_s)

    assert InstalledStopTimeout(installed).problem(_BUDGET) == ""
    assert InstalledStopTimeout(installed).problem(_BUDGET * 3) != ""


def test_nothing_installed_gives_no_verdict(tmp_path: Path) -> None:
    assert InstalledStopTimeout(tmp_path / "absent.plist").problem(_BUDGET) == ""


def test_an_unparseable_plist_counts_as_carrying_none(tmp_path: Path) -> None:
    broken = tmp_path / "q.plist"
    broken.write_text("not a plist")

    assert "has none" in InstalledStopTimeout(broken).problem(_BUDGET)


def test_doctor_flags_an_old_installed_service(
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
    assert str(_POLICY.stop_timeout_s) in check.message  # from the env override


def test_doctor_passes_a_current_installed_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = _plist(tmp_path / "quarry.plist", ExitTimeOut=_POLICY.stop_timeout_s)
    monkeypatch.setenv(ShutdownPolicy.ENV_NAME, f"{_BUDGET:g}")

    with patch.object(
        InstalledStopTimeout, "installed", return_value=InstalledStopTimeout(current)
    ):
        check = DaemonDiagnostics.stop_timeout()

    assert check.passed


def test_the_daemon_logs_an_error_at_start_for_an_old_installed_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    old = _plist(tmp_path / "quarry.plist")
    monkeypatch.setenv(ShutdownPolicy.ENV_NAME, f"{_BUDGET:g}")
    options = BindOptions(
        host="127.0.0.1", port=8420, db="", api_key=None, cors_origins=(), tls=False
    )

    with (
        patch("quarry.daemon.launcher.DaemonServer.serve"),
        patch.object(
            InstalledStopTimeout, "installed", return_value=InstalledStopTimeout(old)
        ),
        caplog.at_level(logging.ERROR, logger="quarry.installed_stop_timeout"),
    ):
        DaemonLauncher(options).launch()

    assert "quarry install" in caplog.text
    assert str(_POLICY.stop_timeout_s) in caplog.text  # the env override flowed in
