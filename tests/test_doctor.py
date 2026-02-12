from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    import pytest

from quarry.doctor import (
    _check_aws_credentials,
    _check_data_directory,
    _check_embedding_model,
    _check_imports,
    _check_local_ocr,
    _check_python_version,
    _configure_claude_code,
    _configure_claude_desktop,
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
    def test_existing_writable_directory(self, tmp_path, monkeypatch):
        data_dir = tmp_path / ".quarry" / "data" / "lancedb"
        data_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = _check_data_directory()
        assert result.passed is True

    def test_missing_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = _check_data_directory()
        assert result.passed is False
        assert "does not exist" in result.message


class TestCheckAwsCredentials:
    def test_credentials_via_env(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCY")
        result = _check_aws_credentials()
        assert result.passed is True
        assert "AKIA" in result.message
        assert "MPLE" in result.message
        assert "env" in result.message.lower() or "via" in result.message.lower()

    def test_no_credentials(self, monkeypatch):
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
        result = _check_aws_credentials()
        assert result.passed is False
        assert result.required is False
        assert "Not configured" in result.message


class TestCheckEmbeddingModel:
    def test_model_cached(self, tmp_path, monkeypatch):
        model_dir = (
            tmp_path
            / ".cache"
            / "huggingface"
            / "hub"
            / "models--Snowflake--snowflake-arctic-embed-m-v1.5"
        )
        model_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = _check_embedding_model()
        assert result.passed is True

    def test_model_not_cached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
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


class TestCheckEnvironment:
    def test_returns_zero_when_all_pass(self, tmp_path, monkeypatch):
        data_dir = tmp_path / ".quarry" / "data" / "lancedb"
        data_dir.mkdir(parents=True)
        model_dir = (
            tmp_path
            / ".cache"
            / "huggingface"
            / "hub"
            / "models--Snowflake--snowflake-arctic-embed-m-v1.5"
        )
        model_dir.mkdir(parents=True)
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
        assert check_environment() == 0

    def test_returns_one_when_required_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
        # AWS is now optional, so only data_directory check fails
        assert check_environment() == 1


def _mock_install_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out MCP configuration for install tests."""
    import quarry.doctor as doctor_mod

    noop = lambda: doctor_mod.CheckResult(  # noqa: E731
        name="stub", passed=True, message="mocked"
    )
    monkeypatch.setattr(doctor_mod, "_configure_claude_code", noop)
    monkeypatch.setattr(doctor_mod, "_configure_claude_desktop", noop)


class TestConfigureClaudeCode:
    def test_claude_not_on_path(self, monkeypatch):
        monkeypatch.setattr("quarry.doctor.shutil.which", lambda _name: None)
        result = _configure_claude_code()
        assert result.passed is False
        assert "not found" in result.message

    def test_claude_mcp_add_succeeds(self, monkeypatch):
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

    def test_claude_mcp_add_already_exists(self, monkeypatch):
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

    def test_claude_mcp_add_fails(self, monkeypatch):
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
    def test_desktop_not_installed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "quarry.doctor._DESKTOP_CONFIG_PATH",
            tmp_path / "nonexistent" / "config.json",
        )
        result = _configure_claude_desktop()
        assert result.passed is False
        assert "not installed" in result.message

    def test_creates_new_config(self, tmp_path, monkeypatch):
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

    def test_preserves_existing_servers(self, tmp_path, monkeypatch):
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
    def test_creates_data_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _mock_install_mcp(monkeypatch)
        with patch("sentence_transformers.SentenceTransformer") as mock_st:
            mock_st.return_value = None
            result = run_install()
        assert result == 0
        assert (tmp_path / ".quarry" / "data" / "lancedb").is_dir()

    def test_downloads_model(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _mock_install_mcp(monkeypatch)
        with patch("sentence_transformers.SentenceTransformer") as mock_st:
            mock_st.return_value = None
            run_install()
        from quarry.config import EMBEDDING_MODEL_REVISION

        mock_st.assert_called_once_with(
            "Snowflake/snowflake-arctic-embed-m-v1.5",
            revision=EMBEDDING_MODEL_REVISION,
        )

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _mock_install_mcp(monkeypatch)
        data_dir = tmp_path / ".quarry" / "data" / "lancedb"
        data_dir.mkdir(parents=True)
        with patch("sentence_transformers.SentenceTransformer") as mock_st:
            mock_st.return_value = None
            result = run_install()
        assert result == 0
        assert data_dir.is_dir()
