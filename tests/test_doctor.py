from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    import pytest

    MP = pytest.MonkeyPatch

from quarry.doctor import (
    _check_aws_credentials,
    _check_claude_code_mcp,
    _check_claude_desktop_mcp,
    _check_data_directory,
    _check_embedding_model,
    _check_imports,
    _check_local_ocr,
    _check_python_version,
    _check_storage,
    _configure_claude_code,
    _configure_claude_desktop,
    _human_size,
    _quiet_logging,
    check_environment,
    run_install,
)


class TestCheckPythonVersion:
    def test_always_passes(self):
        result = _check_python_version()
        assert result.passed is True
        assert result.required is False
        assert "." in result.message


class TestCheckDataDirectory:
    def test_existing_writable_directory(self, tmp_path: Path, monkeypatch: MP):
        data_dir = tmp_path / ".quarry" / "data" / "default" / "lancedb"
        data_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = _check_data_directory()
        assert result.passed is True

    def test_missing_directory(self, tmp_path: Path, monkeypatch: MP):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = _check_data_directory()
        assert result.passed is False
        assert "does not exist" in result.message


class TestCheckAwsCredentials:
    def test_credentials_via_env(self, monkeypatch: MP):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCY")
        result = _check_aws_credentials()
        assert result.passed is True
        assert "AKIA" in result.message
        assert "MPLE" in result.message
        assert "env" in result.message.lower() or "via" in result.message.lower()

    def test_no_credentials(self, monkeypatch: MP):
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
        result = _check_aws_credentials()
        assert result.passed is False
        assert result.required is False
        assert "Not configured" in result.message


class TestCheckEmbeddingModel:
    def test_both_files_cached(self, tmp_path: Path):
        model_path = tmp_path / "model_int8.onnx"
        tokenizer_path = tmp_path / "tokenizer.json"
        model_path.write_bytes(b"fake")
        tokenizer_path.write_bytes(b"fake")

        def _mock_cache(repo_id: str, filename: str, **kwargs: object) -> str:
            if "model_int8" in filename:
                return str(model_path)
            return str(tokenizer_path)

        with patch(
            "huggingface_hub.try_to_load_from_cache",
            side_effect=_mock_cache,
        ):
            result = _check_embedding_model()
        assert result.passed is True
        assert "ONNX" in result.message

    def test_model_not_cached(self):
        with patch(
            "huggingface_hub.try_to_load_from_cache",
            return_value=None,
        ):
            result = _check_embedding_model()
        assert result.passed is False
        assert "Not cached" in result.message

    def test_tokenizer_not_cached(self, tmp_path: Path):
        model_path = tmp_path / "model_int8.onnx"
        model_path.write_bytes(b"fake")

        def _mock_cache(repo_id: str, filename: str, **kwargs: object) -> str | None:
            if "model_int8" in filename:
                return str(model_path)
            return None

        with patch(
            "huggingface_hub.try_to_load_from_cache",
            side_effect=_mock_cache,
        ):
            result = _check_embedding_model()
        assert result.passed is False
        assert "Not cached" in result.message


class TestCheckImports:
    def test_checks_core_and_ocr_modules(self):
        result = _check_imports()
        # Verifies function runs without error; pass/fail depends on env
        assert result.name == "Core imports"
        assert "modules OK" in result.message or "Failed" in result.message

    def test_missing_import(self):
        import builtins

        real_import = builtins.__import__

        def mock_import(
            name: str,
            globals: dict[str, object] | None = None,
            locals: dict[str, object] | None = None,
            fromlist: list[str] = [],  # noqa: B006
            level: int = 0,
        ) -> object:
            if name == "lancedb":
                raise ImportError("No module named 'lancedb'")
            return real_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=mock_import):
            result = _check_imports()
        assert result.passed is False
        assert "lancedb" in result.message


class TestCheckLocalOcr:
    def test_reports_result(self):
        result = _check_local_ocr()
        assert result.name == "Local OCR"
        # Pass/fail depends on whether rapidocr is installed in test env
        if result.passed:
            assert "RapidOCR" in result.message


class TestCheckStorage:
    def test_reports_size(self, tmp_path: Path, monkeypatch: MP):
        data_dir = tmp_path / ".quarry" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "test.db").write_bytes(b"x" * 1024)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = _check_storage()
        assert result.passed is True
        assert result.required is False
        assert "KB" in result.message

    def test_no_data_dir(self, tmp_path: Path, monkeypatch: MP):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = _check_storage()
        assert result.passed is True
        assert "no data yet" in result.message


class TestCheckClaudeCodeMcp:
    def test_claude_not_on_path(self, monkeypatch: MP):
        monkeypatch.setattr("quarry.doctor.shutil.which", lambda _name: None)
        result = _check_claude_code_mcp()
        assert result.passed is False
        assert result.required is False
        assert "not found" in result.message

    def test_quarry_configured(self, monkeypatch: MP):
        monkeypatch.setattr(
            "quarry.doctor.shutil.which", lambda _name: "/usr/bin/claude"
        )
        mock_result = type(
            "CompletedProcess",
            (),
            {"returncode": 0, "stdout": "quarry: uvx quarry mcp", "stderr": ""},
        )()
        monkeypatch.setattr(
            "quarry.doctor.subprocess.run", lambda *_a, **_kw: mock_result
        )
        result = _check_claude_code_mcp()
        assert result.passed is True
        assert "configured" in result.message

    def test_quarry_not_configured(self, monkeypatch: MP):
        monkeypatch.setattr(
            "quarry.doctor.shutil.which", lambda _name: "/usr/bin/claude"
        )
        mock_result = type(
            "CompletedProcess",
            (),
            {"returncode": 0, "stdout": "other-tool: npx other", "stderr": ""},
        )()
        monkeypatch.setattr(
            "quarry.doctor.subprocess.run", lambda *_a, **_kw: mock_result
        )
        result = _check_claude_code_mcp()
        assert result.passed is False
        assert "not configured" in result.message


class TestCheckClaudeDesktopMcp:
    def test_desktop_not_installed(self, tmp_path: Path, monkeypatch: MP):
        monkeypatch.setattr(
            "quarry.doctor._DESKTOP_CONFIG_PATH",
            tmp_path / "nonexistent" / "config.json",
        )
        result = _check_claude_desktop_mcp()
        assert result.passed is False
        assert "not installed" in result.message

    def test_quarry_configured(self, tmp_path: Path, monkeypatch: MP):
        config_path = tmp_path / "claude_desktop_config.json"
        config = {"mcpServers": {"quarry": {"command": "uvx", "args": []}}}
        config_path.write_text(json.dumps(config))
        monkeypatch.setattr("quarry.doctor._DESKTOP_CONFIG_PATH", config_path)
        result = _check_claude_desktop_mcp()
        assert result.passed is True
        assert "configured" in result.message

    def test_quarry_not_configured(self, tmp_path: Path, monkeypatch: MP):
        config_path = tmp_path / "claude_desktop_config.json"
        config = {"mcpServers": {"other": {"command": "npx", "args": []}}}
        config_path.write_text(json.dumps(config))
        monkeypatch.setattr("quarry.doctor._DESKTOP_CONFIG_PATH", config_path)
        result = _check_claude_desktop_mcp()
        assert result.passed is False
        assert "not configured" in result.message

    def test_no_config_file(self, tmp_path: Path, monkeypatch: MP):
        config_path = tmp_path / "claude_desktop_config.json"
        monkeypatch.setattr("quarry.doctor._DESKTOP_CONFIG_PATH", config_path)
        result = _check_claude_desktop_mcp()
        assert result.passed is False
        assert "no config file" in result.message


class TestHumanSize:
    def test_bytes(self):
        assert "500" in _human_size(500)
        assert "B" in _human_size(500)

    def test_kilobytes(self):
        result = _human_size(2048)
        assert "KB" in result

    def test_megabytes(self):
        result = _human_size(5 * 1024 * 1024)
        assert "MB" in result

    def test_gigabytes(self):
        result = _human_size(2 * 1024 * 1024 * 1024)
        assert "GB" in result


class TestQuietLogging:
    def test_suppresses_logging(self):
        import logging

        with _quiet_logging():
            assert logging.getLogger().level == logging.CRITICAL
        # Level restored after context
        assert logging.getLogger().level != logging.CRITICAL


class TestCheckEnvironment:
    def test_returns_zero_when_all_pass(self, tmp_path: Path, monkeypatch: MP):
        data_dir = tmp_path / ".quarry" / "data" / "default" / "lancedb"
        data_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        import quarry.doctor as doctor_mod

        _ok = doctor_mod.CheckResult
        monkeypatch.setattr(
            doctor_mod,
            "_check_local_ocr",
            lambda: _ok(name="Local OCR", passed=True, message="mocked"),
        )
        monkeypatch.setattr(
            doctor_mod,
            "_check_imports",
            lambda: _ok(name="Core imports", passed=True, message="mocked"),
        )
        monkeypatch.setattr(
            doctor_mod,
            "_check_embedding_model",
            lambda: _ok(name="Embedding model", passed=True, message="mocked"),
        )
        assert check_environment() == 0

    def test_returns_one_when_required_fails(self, tmp_path: Path, monkeypatch: MP):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
        # AWS is now optional, so only data_directory check fails
        assert check_environment() == 1


def _mock_install_deps(monkeypatch: MP) -> None:
    """Stub out MCP configuration and check_environment for install tests."""
    import quarry.doctor as doctor_mod

    noop = lambda: doctor_mod.CheckResult(  # noqa: E731
        name="stub", passed=True, message="mocked"
    )
    monkeypatch.setattr(doctor_mod, "_configure_claude_code", noop)
    monkeypatch.setattr(doctor_mod, "_configure_claude_desktop", noop)
    monkeypatch.setattr(doctor_mod, "check_environment", lambda **_kw: 0)


class TestConfigureClaudeCode:
    def test_claude_not_on_path(self, monkeypatch: MP):
        monkeypatch.setattr("quarry.doctor.shutil.which", lambda _name: None)
        result = _configure_claude_code()
        assert result.passed is False
        assert "not found" in result.message

    def test_claude_mcp_add_succeeds(self, monkeypatch: MP):
        monkeypatch.setattr(
            "quarry.doctor.shutil.which", lambda _name: "/usr/bin/claude"
        )
        mock_result = type(
            "CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )()
        monkeypatch.setattr(
            "quarry.doctor.subprocess.run", lambda *_a, **_kw: mock_result
        )
        result = _configure_claude_code()
        assert result.passed is True
        assert "configured" in result.message

    def test_claude_mcp_add_already_exists(self, monkeypatch: MP):
        monkeypatch.setattr(
            "quarry.doctor.shutil.which", lambda _name: "/usr/bin/claude"
        )
        mock_result = type(
            "CompletedProcess",
            (),
            {"returncode": 1, "stdout": "", "stderr": "already exists"},
        )()
        monkeypatch.setattr(
            "quarry.doctor.subprocess.run", lambda *_a, **_kw: mock_result
        )
        result = _configure_claude_code()
        assert result.passed is True
        assert "already configured" in result.message

    def test_claude_mcp_add_fails(self, monkeypatch: MP):
        monkeypatch.setattr(
            "quarry.doctor.shutil.which", lambda _name: "/usr/bin/claude"
        )
        mock_result = type(
            "CompletedProcess",
            (),
            {"returncode": 1, "stdout": "", "stderr": "permission denied"},
        )()
        monkeypatch.setattr(
            "quarry.doctor.subprocess.run", lambda *_a, **_kw: mock_result
        )
        result = _configure_claude_code()
        assert result.passed is False
        assert "permission denied" in result.message


class TestConfigureClaudeDesktop:
    def test_desktop_not_installed(self, tmp_path: Path, monkeypatch: MP):
        monkeypatch.setattr(
            "quarry.doctor._DESKTOP_CONFIG_PATH",
            tmp_path / "nonexistent" / "config.json",
        )
        result = _configure_claude_desktop()
        assert result.passed is False
        assert "not installed" in result.message

    def test_creates_new_config(self, tmp_path: Path, monkeypatch: MP):
        config_path = tmp_path / "claude_desktop_config.json"
        monkeypatch.setattr("quarry.doctor._DESKTOP_CONFIG_PATH", config_path)
        result = _configure_claude_desktop()
        assert result.passed is True
        config = json.loads(config_path.read_text())
        command = config["mcpServers"]["quarry"]["command"]
        assert command.endswith("uvx")
        assert config["mcpServers"]["quarry"]["args"] == [
            "--from",
            "quarry-mcp",
            "quarry",
            "mcp",
        ]

    def test_preserves_existing_servers(self, tmp_path: Path, monkeypatch: MP):
        config_path = tmp_path / "claude_desktop_config.json"
        existing = {"mcpServers": {"other": {"command": "npx", "args": ["other"]}}}
        config_path.write_text(json.dumps(existing))
        monkeypatch.setattr("quarry.doctor._DESKTOP_CONFIG_PATH", config_path)
        result = _configure_claude_desktop()
        assert result.passed is True
        config = json.loads(config_path.read_text())
        assert "other" in config["mcpServers"]
        assert "quarry" in config["mcpServers"]


class TestRunInstall:
    def test_creates_data_directory(self, tmp_path: Path, monkeypatch: MP):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _mock_install_deps(monkeypatch)
        with patch("quarry.embeddings._download_model_files") as mock_dl:
            mock_dl.return_value = ("/fake/model.onnx", "/fake/tokenizer.json")
            result = run_install()
        assert result == 0
        assert (tmp_path / ".quarry" / "data" / "default" / "lancedb").is_dir()

    def test_downloads_model(self, tmp_path: Path, monkeypatch: MP):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _mock_install_deps(monkeypatch)
        with patch("quarry.embeddings._download_model_files") as mock_dl:
            mock_dl.return_value = ("/fake/model.onnx", "/fake/tokenizer.json")
            run_install()
        mock_dl.assert_called_once()

    def test_idempotent(self, tmp_path: Path, monkeypatch: MP):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _mock_install_deps(monkeypatch)
        data_dir = tmp_path / ".quarry" / "data" / "default" / "lancedb"
        data_dir.mkdir(parents=True)
        with patch("quarry.embeddings._download_model_files") as mock_dl:
            mock_dl.return_value = ("/fake/model.onnx", "/fake/tokenizer.json")
            result = run_install()
        assert result == 0
        assert data_dir.is_dir()

    def test_model_download_failure_returns_one(self, tmp_path: Path, monkeypatch: MP):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _mock_install_deps(monkeypatch)
        with patch(
            "quarry.embeddings._download_model_files",
            side_effect=RuntimeError("network error"),
        ):
            result = run_install()
        assert result == 1
