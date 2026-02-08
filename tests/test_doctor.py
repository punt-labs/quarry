from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from quarry.doctor import (
    _check_aws_credentials,
    _check_data_directory,
    _check_embedding_model,
    _check_imports,
    _check_python_version,
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
        assert "No credentials" in result.message


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
    def test_all_imports_available(self):
        result = _check_imports()
        assert result.passed is True
        assert "5 modules OK" in result.message

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
        assert check_environment() == 0

    def test_returns_one_when_required_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
        assert check_environment() == 1


class TestRunInstall:
    def test_creates_data_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with patch("sentence_transformers.SentenceTransformer") as mock_st:
            mock_st.return_value = None
            result = run_install()
        assert result == 0
        assert (tmp_path / ".quarry" / "data" / "lancedb").is_dir()

    def test_downloads_model(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with patch("sentence_transformers.SentenceTransformer") as mock_st:
            mock_st.return_value = None
            run_install()
        mock_st.assert_called_once_with("Snowflake/snowflake-arctic-embed-m-v1.5")

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        data_dir = tmp_path / ".quarry" / "data" / "lancedb"
        data_dir.mkdir(parents=True)
        with patch("sentence_transformers.SentenceTransformer") as mock_st:
            mock_st.return_value = None
            result = run_install()
        assert result == 0
        assert data_dir.is_dir()
