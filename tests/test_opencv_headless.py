"""Tests for the headless-OpenCV enforcer used by ``quarry install``."""

from __future__ import annotations

import subprocess

import pytest

from quarry.opencv_headless import HeadlessOpenCv


def test_command_prefers_uv_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("quarry.opencv_headless.shutil.which", lambda _: "/usr/bin/uv")
    cmd = HeadlessOpenCv("/venv/bin/python")._reinstall_command()
    assert cmd[:5] == ["/usr/bin/uv", "pip", "install", "--python", "/venv/bin/python"]
    assert cmd[-3:] == ["--force-reinstall", "--no-deps", "opencv-python-headless"]


def test_command_falls_back_to_pip_when_no_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("quarry.opencv_headless.shutil.which", lambda _: None)
    cmd = HeadlessOpenCv("/venv/bin/python")._reinstall_command()
    assert cmd == [
        "/venv/bin/python",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--force-reinstall",
        "--no-deps",
        "opencv-python-headless",
    ]


def test_remediation_is_the_reinstall_command_as_a_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The operator hint printed by `quarry install` must be the actual command.
    monkeypatch.setattr("quarry.opencv_headless.shutil.which", lambda _: None)
    hint = HeadlessOpenCv("/venv/bin/python").remediation()
    assert hint == (
        "/venv/bin/python -m pip install --disable-pip-version-check --no-input "
        "--force-reinstall --no-deps opencv-python-headless"
    )


def test_enforce_runs_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("quarry.opencv_headless.shutil.which", lambda _: None)
    monkeypatch.setattr(subprocess, "run", fake_run)
    msg = HeadlessOpenCv("/venv/bin/python").enforce()
    assert recorded and recorded[0][-1] == "opencv-python-headless"
    assert "opencv-python-headless" in msg


def test_enforce_never_blocks_on_input_or_runs_unbounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("quarry.opencv_headless.shutil.which", lambda _: None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    HeadlessOpenCv("/venv/bin/python").enforce()

    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["timeout"] == 300.0


def test_a_failed_reinstall_names_the_error_line_not_the_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = (
        "Collecting opencv-python-headless\n"
        "ERROR: no matching distribution found\n"
        "[notice] A new release of pip is available\n"
    )

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        return subprocess.CompletedProcess(cmd, 1, "", stderr)

    monkeypatch.setattr("quarry.opencv_headless.shutil.which", lambda _: None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="exited 1: ERROR: no matching") as raised:
        HeadlessOpenCv("/venv/bin/python").enforce()

    assert "[notice]" not in str(raised.value)


def test_without_an_error_line_the_last_non_notice_line_is_the_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        return subprocess.CompletedProcess(cmd, 1, "", "disk full\n[notice] upgrade\n")

    monkeypatch.setattr("quarry.opencv_headless.shutil.which", lambda _: None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="exited 1: disk full"):
        HeadlessOpenCv("/venv/bin/python").enforce()


def test_a_reinstall_that_times_out_is_a_timeout_not_a_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd, 300)

    monkeypatch.setattr("quarry.opencv_headless.shutil.which", lambda _: None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        HeadlessOpenCv("/venv/bin/python").enforce()
