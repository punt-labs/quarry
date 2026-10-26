"""Doctor and install always complete; a failed or crashed required check fails them."""

from __future__ import annotations

import errno
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from quarry import doctor as doctor_mod
from quarry.doctor import check_environment, run_install
from quarry.doctor_daemon import DaemonDiagnostics
from quarry.doctor_failure import CheckFailure
from quarry.doctor_mcp import ClaudeCodeMcp, ClaudeDesktopMcp
from tests.test_doctor import _mock_install_deps

MP = pytest.MonkeyPatch


class TestRequiredChecksStayRequired:
    def test_a_required_check_that_crashes_fails_the_run(
        self, tmp_path: Path, monkeypatch: MP, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        def boom() -> object:
            msg = "version probe exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(doctor_mod, "_check_python_version", boom)

        code = check_environment()

        out = capsys.readouterr().out
        assert code == 1
        assert "✗ Python" in out  # a required failure, not an advisory circle

    def test_an_advisory_check_that_crashes_does_not_fail_the_run(
        self, tmp_path: Path, monkeypatch: MP, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        baseline = check_environment()
        capsys.readouterr()

        def boom() -> object:
            msg = "probe exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(DaemonDiagnostics, "reachability", boom)

        assert check_environment() == baseline


class TestHomeAndCwdAreGuarded:
    def test_a_deleted_working_directory_is_a_failed_check_not_a_crash(
        self, tmp_path: Path, monkeypatch: MP, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        def gone() -> Path:
            raise FileNotFoundError(errno.ENOENT, "cwd deleted")

        monkeypatch.setattr(Path, "cwd", gone)

        check_environment()

        out = capsys.readouterr().out
        assert "FileNotFoundError" in out
        assert "Recall telemetry" in out  # the checks after it still ran

    def test_a_home_that_cannot_be_resolved_does_not_crash_doctor_or_install(
        self, tmp_path: Path, monkeypatch: MP, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def no_home() -> Path:
            msg = "Could not determine home directory."
            raise RuntimeError(msg)

        monkeypatch.setattr(Path, "home", no_home)

        check_environment()
        out = capsys.readouterr().out

        assert "RuntimeError" in out
        assert "Python" in out


@pytest.mark.usefixtures("real_embedding_factory")
class TestInstallAlwaysCompletes:
    _DL = "quarry.embeddings.OnnxEmbeddingBackend.download_model_files"

    def test_a_configure_that_crashes_is_a_failed_step_and_install_goes_on(
        self, tmp_path: Path, monkeypatch: MP, capsys: pytest.CaptureFixture[str]
    ) -> None:
        registered: list[bool] = []

        def register() -> str:
            registered.append(True)
            return "registered"

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _mock_install_deps(monkeypatch)

        def boom(_self: ClaudeCodeMcp) -> object:
            msg = "claude exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(ClaudeCodeMcp, "configure", boom)
        monkeypatch.setattr("quarry.service.install", register)
        with patch(self._DL) as download:
            download.return_value = ("/m.onnx", "/t.json")
            run_install()

        assert registered == [True]
        assert "RuntimeError" in capsys.readouterr().out

    def test_a_home_that_cannot_be_resolved_fails_the_directory_step_only(
        self, tmp_path: Path, monkeypatch: MP, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _mock_install_deps(monkeypatch)

        def no_home() -> Path:
            msg = "no home"
            raise RuntimeError(msg)

        monkeypatch.setattr(Path, "home", no_home)
        with patch(self._DL) as download:
            download.return_value = ("/m.onnx", "/t.json")
            code = run_install()

        assert code == 1
        assert "Failed to create directories: RuntimeError" in capsys.readouterr().out

    def test_the_fp16_step_says_why_it_was_skipped(
        self, tmp_path: Path, monkeypatch: MP, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _mock_install_deps(monkeypatch)
        monkeypatch.setattr(
            "onnxruntime.get_available_providers", lambda: ["CUDAExecutionProvider"]
        )
        calls: list[object] = []

        def download(**kwargs: object) -> tuple[str, str]:
            calls.append(kwargs)
            if kwargs.get("model_file") == "onnx/model_fp16.onnx":
                raise OSError(errno.ENOSPC, "no space")
            return ("/m.onnx", "/t.json")

        with patch(self._DL, side_effect=download):
            run_install()

        out = capsys.readouterr().out
        assert "FP16 skipped: OSError: ENOSPC" in out
        assert len(calls) == 2


def _fake_claude(directory: Path, script: str) -> Path:
    path = directory / "claude"
    path.write_text(f"#!/bin/sh\n{script}\n")
    path.chmod(0o755)
    return path


class TestClaudeCodeConfigure:
    def test_output_that_is_not_utf_8_is_a_failed_result_not_a_crash(
        self, tmp_path: Path, monkeypatch: MP
    ) -> None:
        _fake_claude(tmp_path, "printf '\\377\\376 broken' >&2; exit 1")
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

        result = ClaudeCodeMcp().configure()

        assert result.passed is False
        assert "claude mcp add failed" in result.message

    def test_what_claude_says_is_one_bounded_printable_line(
        self, tmp_path: Path, monkeypatch: MP
    ) -> None:
        noise = "x" * 500
        _fake_claude(tmp_path, f"printf 'first\\033[31m line\\n{noise}' >&2; exit 1")
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

        result = ClaudeCodeMcp().configure()

        assert "\x1b" not in result.message
        assert "x" * 200 not in result.message
        assert "first" in result.message

    def test_a_re_add_that_times_out_after_the_remove_is_the_loud_message(
        self, monkeypatch: MP
    ) -> None:
        monkeypatch.setattr(
            "quarry.doctor_mcp.shutil.which", lambda _name: "/usr/bin/claude"
        )
        calls: list[list[str]] = []

        def run(argv: list[str], **_kw: object) -> object:
            calls.append(argv)
            if len(calls) == 3:
                raise subprocess.TimeoutExpired(argv, 1)
            stderr = "already exists" if len(calls) == 1 else ""
            code = 1 if len(calls) == 1 else 0
            return type(
                "CP", (), {"returncode": code, "stdout": "", "stderr": stderr}
            )()

        monkeypatch.setattr("quarry.doctor_mcp.subprocess.run", run)

        result = ClaudeCodeMcp().configure()

        assert result.passed is False
        assert "removed the stale quarry MCP entry but the re-add failed" in (
            result.message
        )
        assert "TimeoutExpired" in result.message

    def test_the_calls_share_one_total_time_bound(self, monkeypatch: MP) -> None:
        monkeypatch.setattr(
            "quarry.doctor_mcp.shutil.which", lambda _name: "/usr/bin/claude"
        )
        timeouts: list[float] = []

        def run(_argv: list[str], **kw: float) -> object:
            timeouts.append(kw["timeout"])
            return type(
                "CP", (), {"returncode": 1, "stdout": "", "stderr": "already exists"}
            )()

        monkeypatch.setattr("quarry.doctor_mcp.subprocess.run", run)
        clock = iter([0.0, 0.0, 100.0, 100.0, 119.0, 119.0, 130.0, 130.0])
        monkeypatch.setattr("quarry.doctor_mcp.time.monotonic", lambda: next(clock))

        result = ClaudeCodeMcp().configure()

        assert result.passed is False
        assert all(0 < t <= 60.0 for t in timeouts)

    def test_an_attribute_error_in_the_check_is_not_swallowed(
        self, tmp_path: Path
    ) -> None:
        registry = tmp_path / "installed_plugins.json"
        registry.write_text("{}")
        with (
            patch.object(ClaudeCodeMcp, "_read_registry", side_effect=AttributeError),
            pytest.raises(AttributeError),
        ):
            ClaudeCodeMcp(registry).check()


class TestDesktopFailures:
    def test_an_untraversable_parent_is_a_failed_result(self, tmp_path: Path) -> None:
        locked = tmp_path / "locked"
        locked.mkdir()
        config = locked / "Claude" / "claude_desktop_config.json"
        locked.chmod(0)
        try:
            configured = ClaudeDesktopMcp(config).configure()
            checked = ClaudeDesktopMcp(config).check()
        finally:
            locked.chmod(0o700)

        assert configured.passed is False
        assert checked.passed is False

    def test_quarry_that_is_not_on_path_is_never_registered_as_configured(
        self, tmp_path: Path, monkeypatch: MP
    ) -> None:
        monkeypatch.setattr("quarry.doctor_mcp.shutil.which", lambda _name: None)
        config = tmp_path / "claude_desktop_config.json"
        config.write_text("{}")

        result = ClaudeDesktopMcp(config).configure()

        assert result.passed is False
        assert "not on PATH" in result.message
        assert config.read_text() == "{}"


class TestCheckFailureMessages:
    def test_an_oserror_is_shown_by_errno_name_never_its_path(self) -> None:
        exc = OSError(errno.EACCES, "denied", "/home/someone/secret/path")

        assert CheckFailure.detail(exc) == "PermissionError: EACCES"

    def test_a_raising_str_is_survived(self) -> None:
        class HostileError(Exception):
            def __str__(self) -> str:
                msg = "no str for you"
                raise RuntimeError(msg)

        assert CheckFailure.detail(HostileError()) == "HostileError"

    def test_an_import_errors_module_name_is_sanitized(self) -> None:
        exc = ImportError("x", name="evil\x1b[31m\nsecond line")

        message = CheckFailure.import_message("attempted", exc)

        assert "\x1b" not in message
        assert "second line" not in message

    def test_a_non_import_error_names_the_module_it_was_raised_in(self) -> None:
        def raiser() -> None:
            msg = "bad abi"
            raise ValueError(msg)

        with pytest.raises(ValueError, match="bad abi") as raised:
            raiser()

        message = CheckFailure.import_message("quarry.db", raised.value)

        assert "cannot load quarry.db" in message
        assert "in tests.test_doctor_contract" in message

    def test_a_missing_module_advises_reinstalling_and_others_do_not(self) -> None:
        missing = ModuleNotFoundError("gone", name="libfoo")

        assert "reinstall quarry" in CheckFailure.import_message("x", missing)
        assert "reinstall" not in CheckFailure.import_message("x", OSError("y"))
