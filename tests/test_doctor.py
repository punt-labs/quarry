from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    import pytest

    MP = pytest.MonkeyPatch

from quarry.doctor import (
    _check_claude_code_mcp,
    _check_claude_desktop_mcp,
    _check_data_directory,
    _check_embedding_model,
    _check_fts_health,
    _check_imports,
    _check_local_ocr,
    _check_provider,
    _check_python_version,
    _check_storage,
    _check_sync_directories,
    _check_sync_health,
    _configure_claude_code,
    _configure_claude_desktop,
    _configure_ethos_ext,
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
        data_dir = tmp_path / ".punt-labs" / "quarry" / "data" / "default" / "lancedb"
        data_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = _check_data_directory()
        assert result.passed is True

    def test_missing_directory(self, tmp_path: Path, monkeypatch: MP):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = _check_data_directory()
        assert result.passed is False
        assert "does not exist" in result.message


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


class TestCheckProvider:
    def test_reports_provider_on_success(self) -> None:
        from quarry.provider import ProviderSelection

        selection = ProviderSelection(
            provider="CPUExecutionProvider",
            model_file="onnx/model_int8.onnx",
        )
        with patch("quarry.provider.select_provider", return_value=selection):
            result = _check_provider()
        assert result.passed is True
        assert result.required is False
        assert result.name == "ONNX provider"
        assert "CPUExecutionProvider" in result.message
        assert "onnx/model_int8.onnx" in result.message

    def test_reports_cuda_provider(self) -> None:
        from quarry.provider import ProviderSelection

        selection = ProviderSelection(
            provider="CUDAExecutionProvider",
            model_file="onnx/model_fp16.onnx",
        )
        with patch("quarry.provider.select_provider", return_value=selection):
            result = _check_provider()
        assert result.passed is True
        assert "CUDAExecutionProvider" in result.message

    def test_reports_failure_on_exception(self) -> None:
        with patch(
            "quarry.provider.select_provider",
            side_effect=RuntimeError("CUDA not available"),
        ):
            result = _check_provider()
        assert result.passed is False
        assert result.required is False
        assert "CUDA not available" in result.message


class TestCheckStorage:
    def test_reports_size(self, tmp_path: Path, monkeypatch: MP):
        data_dir = tmp_path / ".punt-labs" / "quarry" / "data"
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
    """Tests for the file-based Claude Code MCP check.

    The check reads ``~/.claude/plugins/installed_plugins.json`` directly
    instead of shelling out to ``claude mcp list``.
    """

    @staticmethod
    def _write_plugins(
        tmp_path: Path,
        plugins: dict[str, object],
        *,
        version: int = 2,
    ) -> Path:
        """Write an installed_plugins.json under *tmp_path* and return its path."""
        plugins_path = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        plugins_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": version, "plugins": plugins}
        plugins_path.write_text(json.dumps(payload), encoding="utf-8")
        return plugins_path

    @staticmethod
    def _make_plugin_dir(install_path: Path) -> None:
        """Create a minimal ``.claude-plugin/plugin.json`` with an mcpServers entry."""
        plugin_json = install_path / ".claude-plugin" / "plugin.json"
        plugin_json.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "name": "quarry",
            "mcpServers": {
                "quarry": {"type": "stdio", "command": "quarry", "args": ["mcp"]},
            },
        }
        plugin_json.write_text(json.dumps(manifest), encoding="utf-8")

    def test_quarry_configured(self, tmp_path: Path, monkeypatch: MP):
        install_path = tmp_path / "plugins" / "cache" / "punt-labs" / "quarry" / "1.0.0"
        self._make_plugin_dir(install_path)
        plugins_path = self._write_plugins(
            tmp_path,
            {
                "quarry@punt-labs": [
                    {"scope": "user", "installPath": str(install_path)}
                ],
            },
        )
        monkeypatch.setattr("quarry.doctor._CLAUDE_CODE_PLUGINS_PATH", plugins_path)
        result = _check_claude_code_mcp()
        assert result.passed is True
        assert "configured" in result.message

    def test_quarry_not_configured(self, tmp_path: Path, monkeypatch: MP):
        plugins_path = self._write_plugins(
            tmp_path,
            {
                "other@example": [{"scope": "user", "installPath": "/fake"}],
            },
        )
        monkeypatch.setattr("quarry.doctor._CLAUDE_CODE_PLUGINS_PATH", plugins_path)
        result = _check_claude_code_mcp()
        assert result.passed is False
        assert "not configured" in result.message

    def test_no_plugin_registry(self, tmp_path: Path, monkeypatch: MP):
        missing = tmp_path / "nonexistent" / "installed_plugins.json"
        monkeypatch.setattr("quarry.doctor._CLAUDE_CODE_PLUGINS_PATH", missing)
        result = _check_claude_code_mcp()
        assert result.passed is False
        assert result.required is False
        assert "no plugin registry" in result.message

    def test_invalid_json(self, tmp_path: Path, monkeypatch: MP):
        plugins_path = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        plugins_path.parent.mkdir(parents=True, exist_ok=True)
        plugins_path.write_text("{invalid json", encoding="utf-8")
        monkeypatch.setattr("quarry.doctor._CLAUDE_CODE_PLUGINS_PATH", plugins_path)
        result = _check_claude_code_mcp()
        assert result.passed is False
        assert "config error" in result.message

    def test_install_path_missing(self, tmp_path: Path, monkeypatch: MP):
        """Registry lists quarry but the installPath directory doesn't exist."""
        plugins_path = self._write_plugins(
            tmp_path,
            {
                "quarry@punt-labs": [
                    {"scope": "user", "installPath": "/nonexistent/path"}
                ],
            },
        )
        monkeypatch.setattr("quarry.doctor._CLAUDE_CODE_PLUGINS_PATH", plugins_path)
        result = _check_claude_code_mcp()
        assert result.passed is False
        assert "plugin files missing" in result.message

    def test_empty_entries_list(self, tmp_path: Path, monkeypatch: MP):
        """Registry has the key but an empty list of entries."""
        plugins_path = self._write_plugins(tmp_path, {"quarry@punt-labs": []})
        monkeypatch.setattr("quarry.doctor._CLAUDE_CODE_PLUGINS_PATH", plugins_path)
        result = _check_claude_code_mcp()
        assert result.passed is False
        assert "not configured" in result.message

    def test_unexpected_schema_list_returns_error(
        self, tmp_path: Path, monkeypatch: MP
    ):
        """Registry with unexpected shape (list not dict) degrades."""
        plugins_path = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        plugins_path.parent.mkdir(parents=True, exist_ok=True)
        plugins_path.write_text("[]", encoding="utf-8")
        monkeypatch.setattr("quarry.doctor._CLAUDE_CODE_PLUGINS_PATH", plugins_path)
        result = _check_claude_code_mcp()
        assert not result.passed
        assert result.required is False
        assert "config error" in result.message

    def test_unexpected_schema_plugins_not_list(self, tmp_path: Path, monkeypatch: MP):
        """Plugin entry is a string instead of list of dicts."""
        plugins_path = self._write_plugins(
            tmp_path,
            {"quarry@punt-labs": "not-a-list"},
        )
        monkeypatch.setattr("quarry.doctor._CLAUDE_CODE_PLUGINS_PATH", plugins_path)
        result = _check_claude_code_mcp()
        assert not result.passed
        assert result.required is False
        assert "config error" in result.message

    def test_empty_install_path_returns_error(self, tmp_path: Path, monkeypatch: MP):
        """Entry with empty installPath is caught before Path('') becomes '.'."""
        plugins_path = self._write_plugins(
            tmp_path,
            {"quarry@punt-labs": [{"scope": "user", "installPath": ""}]},
        )
        monkeypatch.setattr("quarry.doctor._CLAUDE_CODE_PLUGINS_PATH", plugins_path)
        result = _check_claude_code_mcp()
        assert not result.passed
        assert result.required is False
        assert "empty installPath" in result.message

    def test_missing_install_path_key_returns_error(
        self, tmp_path: Path, monkeypatch: MP
    ):
        """Entry with no installPath key at all is caught."""
        plugins_path = self._write_plugins(
            tmp_path,
            {"quarry@punt-labs": [{"scope": "user"}]},
        )
        monkeypatch.setattr("quarry.doctor._CLAUDE_CODE_PLUGINS_PATH", plugins_path)
        result = _check_claude_code_mcp()
        assert not result.passed
        assert result.required is False
        assert "empty installPath" in result.message

    def test_manifest_missing_mcp_server(self, tmp_path: Path, monkeypatch: MP):
        """plugin.json exists but has no quarry MCP server entry."""
        install_path = tmp_path / "plugins" / "cache" / "punt-labs" / "quarry" / "1.0.0"
        plugin_json = install_path / ".claude-plugin" / "plugin.json"
        plugin_json.parent.mkdir(parents=True, exist_ok=True)
        manifest = {"name": "quarry", "mcpServers": {}}
        plugin_json.write_text(json.dumps(manifest), encoding="utf-8")
        plugins_path = self._write_plugins(
            tmp_path,
            {
                "quarry@punt-labs": [
                    {"scope": "user", "installPath": str(install_path)}
                ],
            },
        )
        monkeypatch.setattr("quarry.doctor._CLAUDE_CODE_PLUGINS_PATH", plugins_path)
        result = _check_claude_code_mcp()
        assert result.passed is False
        assert "manifest missing" in result.message


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
        data_dir = tmp_path / ".punt-labs" / "quarry" / "data" / "default" / "lancedb"
        data_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
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
        monkeypatch.setattr(
            doctor_mod,
            "_check_provider",
            lambda: _ok(
                name="ONNX provider", passed=True, message="mocked", required=False
            ),
        )
        monkeypatch.setattr(
            doctor_mod,
            "_check_claude_code_mcp",
            lambda: _ok(name="Claude Code MCP", passed=True, message="mocked"),
        )
        monkeypatch.setattr(
            doctor_mod,
            "_check_claude_desktop_mcp",
            lambda: _ok(name="Claude Desktop MCP", passed=True, message="mocked"),
        )
        monkeypatch.setattr(
            doctor_mod,
            "_check_fts_health",
            lambda _p: _ok(
                name="FTS index", passed=True, message="mocked", required=False
            ),
        )
        monkeypatch.setattr(
            doctor_mod,
            "_check_sync_health",
            lambda _p: _ok(name="Sync", passed=True, message="mocked", required=False),
        )
        monkeypatch.setattr(
            doctor_mod,
            "_check_sync_directories",
            lambda _p: _ok(
                name="Sync directories",
                passed=True,
                message="mocked",
                required=False,
            ),
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


class TestConfigureEthosExt:
    def _make_ext(self, identities_dir: Path, handle: str) -> Path:
        ext_dir = identities_dir / f"{handle}.ext"
        ext_dir.mkdir(parents=True, exist_ok=True)
        return ext_dir

    def test_writes_session_context_when_missing(self, tmp_path: Path):
        import yaml

        identities_dir = tmp_path / "identities"
        ext_dir = self._make_ext(identities_dir, "claude")
        quarry_yaml = ext_dir / "quarry.yaml"
        quarry_yaml.write_text(
            yaml.dump({"memory_collection": "claude-memories"}), encoding="utf-8"
        )

        result = _configure_ethos_ext(identities_dir=identities_dir)

        assert result.passed is True
        data = yaml.safe_load(quarry_yaml.read_text(encoding="utf-8"))
        assert "session_context" in data
        assert "claude-memories" in data["session_context"]
        assert "claude" in data["session_context"]
        assert "updated" in result.message
        assert "claude" in result.message

    def test_idempotent_when_session_context_exists(self, tmp_path: Path):
        import yaml

        identities_dir = tmp_path / "identities"
        ext_dir = self._make_ext(identities_dir, "jfreeman")
        quarry_yaml = ext_dir / "quarry.yaml"
        original = {
            "memory_collection": "jfreeman-memories",
            "session_context": "existing content, do not overwrite",
        }
        quarry_yaml.write_text(yaml.dump(original), encoding="utf-8")

        result = _configure_ethos_ext(identities_dir=identities_dir)

        data = yaml.safe_load(quarry_yaml.read_text(encoding="utf-8"))
        assert data["session_context"] == "existing content, do not overwrite"
        assert "already" in result.message

    def test_skips_identity_without_quarry_yaml(self, tmp_path: Path):
        identities_dir = tmp_path / "identities"
        self._make_ext(identities_dir, "nomemory")

        result = _configure_ethos_ext(identities_dir=identities_dir)

        assert result.passed is True
        assert "no identities" in result.message

    def test_ethos_not_installed(self, tmp_path: Path):
        result = _configure_ethos_ext(identities_dir=tmp_path / "nonexistent")

        assert result.passed is True
        assert result.required is False
        assert "ethos not installed" in result.message

    def test_two_identities_one_needs_update(self, tmp_path: Path):
        import yaml

        identities_dir = tmp_path / "identities"

        # claude: needs update
        ext_claude = self._make_ext(identities_dir, "claude")
        (ext_claude / "quarry.yaml").write_text(
            yaml.dump({"memory_collection": "claude-col"}), encoding="utf-8"
        )

        # jfreeman: already has session_context
        ext_jf = self._make_ext(identities_dir, "jfreeman")
        (ext_jf / "quarry.yaml").write_text(
            yaml.dump(
                {
                    "memory_collection": "jf-col",
                    "session_context": "already here",
                }
            ),
            encoding="utf-8",
        )

        result = _configure_ethos_ext(identities_dir=identities_dir)

        assert result.passed is True
        assert "updated 1 identity: claude" in result.message
        assert "already set: jfreeman" in result.message

        claude_text = (ext_claude / "quarry.yaml").read_text(encoding="utf-8")
        claude_data = yaml.safe_load(claude_text)
        assert "session_context" in claude_data

        jf_text = (ext_jf / "quarry.yaml").read_text(encoding="utf-8")
        jf_data = yaml.safe_load(jf_text)
        assert jf_data["session_context"] == "already here"

    def test_no_collection_surfaced_in_message(self, tmp_path: Path):
        """quarry.yaml with no memory_collection surfaces in result, not silent."""
        identities_dir = tmp_path / "identities"
        ext_dir = self._make_ext(identities_dir, "ghost")
        (ext_dir / "quarry.yaml").write_text("other_key: value\n", encoding="utf-8")

        result = _configure_ethos_ext(identities_dir=identities_dir)

        assert result.passed is True
        assert "no memory_collection" in result.message
        assert "ghost" in result.message

    def test_per_identity_failure_isolates_others(self, tmp_path: Path):
        """A bad quarry.yaml for one identity does not skip processing the next."""
        import yaml

        identities_dir = tmp_path / "identities"

        # bad: invalid YAML
        bad_dir = self._make_ext(identities_dir, "aardvark")
        (bad_dir / "quarry.yaml").write_bytes(b"key: [\x00invalid")

        # good: valid, needs update
        good_dir = self._make_ext(identities_dir, "zebra")
        (good_dir / "quarry.yaml").write_text(
            yaml.dump({"memory_collection": "z-col"}), encoding="utf-8"
        )

        result = _configure_ethos_ext(identities_dir=identities_dir)

        # zebra should be updated despite aardvark failing
        assert "zebra" in result.message
        assert "errors" in result.message
        assert "aardvark" in result.message
        # error in aardvark → passed=False
        assert result.passed is False

        zebra_text = (good_dir / "quarry.yaml").read_text(encoding="utf-8")
        zebra_data = yaml.safe_load(zebra_text)
        assert "session_context" in zebra_data

    def test_raw_append_preserves_existing_comments(self, tmp_path: Path):
        """Appending session_context must not destroy existing YAML comments."""
        import yaml

        identities_dir = tmp_path / "identities"
        ext_dir = self._make_ext(identities_dir, "tester")
        original_text = "# important comment\nmemory_collection: tester-col\n"
        (ext_dir / "quarry.yaml").write_text(original_text, encoding="utf-8")

        _configure_ethos_ext(identities_dir=identities_dir)

        updated_text = (ext_dir / "quarry.yaml").read_text(encoding="utf-8")
        assert "# important comment" in updated_text
        data = yaml.safe_load(updated_text)
        assert "session_context" in data
        assert "tester-col" in data["session_context"]

    def test_non_mapping_yaml_surfaced_as_no_collection(self, tmp_path: Path):
        """Non-mapping quarry.yaml is treated as no_collection, not failed.

        Isolation is confirmed: the adjacent valid identity still gets updated.
        """
        import yaml

        identities_dir = tmp_path / "identities"

        # list-yaml: valid YAML but not a mapping — should become no_collection
        list_dir = self._make_ext(identities_dir, "listident")
        (list_dir / "quarry.yaml").write_text("- item1\n- item2\n", encoding="utf-8")

        # good: needs session_context — must still be updated despite list_ident
        good_dir = self._make_ext(identities_dir, "zvalid")
        (good_dir / "quarry.yaml").write_text(
            yaml.dump({"memory_collection": "zvalid-col"}), encoding="utf-8"
        )

        result = _configure_ethos_ext(identities_dir=identities_dir)

        # listident should be in no_collection, not in errors
        assert "no memory_collection" in result.message
        assert "listident" in result.message
        assert "errors" not in result.message

        # zvalid must have been updated — isolation confirmed
        assert "zvalid" in result.message
        zvalid_data = yaml.safe_load(
            (good_dir / "quarry.yaml").read_text(encoding="utf-8")
        )
        assert "session_context" in zvalid_data


_INSERT_DIR = (
    "INSERT INTO directories (directory, collection, registered_at) VALUES (?, ?, ?)"
)
_INSERT_FILE = (
    "INSERT INTO files"
    " (path, collection, document_name, mtime, size, ingested_at)"
    " VALUES (?, ?, ?, ?, ?, ?)"
)


class TestCheckFtsHealth:
    """Tests for the FTS index health check."""

    def test_no_database(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nonexistent" / "lancedb"
        result = _check_fts_health(db_path)
        assert result.passed is True
        assert result.required is False
        assert "no database yet" in result.message

    def test_no_table(self, tmp_path: Path) -> None:
        from quarry.database import get_db

        db_path = tmp_path / "lancedb"
        get_db(db_path)  # creates empty db
        result = _check_fts_health(db_path)
        assert result.passed is True
        assert "no table yet" in result.message

    def test_healthy_fts(self, tmp_path: Path) -> None:
        """FTS query succeeds — check reports healthy."""
        from unittest.mock import MagicMock

        db_path = tmp_path / "lancedb"
        db_path.mkdir(parents=True)

        mock_query = MagicMock()
        mock_query.limit.return_value = mock_query
        mock_query.to_list.return_value = []

        mock_table = MagicMock()
        mock_table.search.return_value = mock_query

        mock_db = MagicMock()
        mock_db.list_tables.return_value = MagicMock(tables=["chunks"])
        mock_db.open_table.return_value = mock_table

        with patch("quarry.database.get_db", return_value=mock_db):
            result = _check_fts_health(db_path)
        assert result.passed is True
        assert result.message == "healthy"
        assert result.required is False

    def test_stale_fts_runtime_error(self, tmp_path: Path) -> None:
        """RuntimeError from FTS query means stale index."""
        from unittest.mock import MagicMock

        db_path = tmp_path / "lancedb"
        db_path.mkdir(parents=True)

        mock_query = MagicMock()
        mock_query.limit.return_value = mock_query
        mock_query.to_list.side_effect = RuntimeError("stale fragment")

        mock_table = MagicMock()
        mock_table.search.return_value = mock_query

        mock_db = MagicMock()
        mock_db.list_tables.return_value = MagicMock(tables=["chunks"])
        mock_db.open_table.return_value = mock_table

        with patch("quarry.database.get_db", return_value=mock_db):
            result = _check_fts_health(db_path)
        assert result.passed is False
        assert "stale" in result.message
        assert result.required is False

    def test_missing_fts_os_error(self, tmp_path: Path) -> None:
        """OSError from FTS query means missing index."""
        from unittest.mock import MagicMock

        db_path = tmp_path / "lancedb"
        db_path.mkdir(parents=True)

        mock_query = MagicMock()
        mock_query.limit.return_value = mock_query
        mock_query.to_list.side_effect = OSError("index missing")

        mock_table = MagicMock()
        mock_table.search.return_value = mock_query

        mock_db = MagicMock()
        mock_db.list_tables.return_value = MagicMock(tables=["chunks"])
        mock_db.open_table.return_value = mock_table

        with patch("quarry.database.get_db", return_value=mock_db):
            result = _check_fts_health(db_path)
        assert result.passed is False
        assert "missing" in result.message
        assert result.required is False


class TestCheckSyncHealth:
    """Tests for the sync age health check."""

    def test_no_registry_file(self, tmp_path: Path) -> None:
        registry_path = tmp_path / "nonexistent" / "registry.db"
        result = _check_sync_health(registry_path)
        assert result.passed is True
        assert result.required is False
        assert "no registrations" in result.message

    def test_no_registrations(self, tmp_path: Path) -> None:
        from quarry.sync_registry import open_registry

        registry_path = tmp_path / "registry.db"
        conn = open_registry(registry_path)
        conn.close()
        result = _check_sync_health(registry_path)
        assert result.passed is True
        assert "no registrations" in result.message

    def test_recent_sync(self, tmp_path: Path) -> None:
        """Collections with recent ingested_at report healthy."""
        from datetime import UTC, datetime

        from quarry.sync_registry import open_registry

        registry_path = tmp_path / "registry.db"
        conn = open_registry(registry_path)
        # Insert a registration
        now = datetime.now(UTC).isoformat()
        sync_dir = tmp_path / "docs"
        sync_dir.mkdir()
        conn.execute(
            _INSERT_DIR,
            (str(sync_dir), "test-col", now),
        )
        # Insert a file record with recent ingested_at
        conn.execute(
            _INSERT_FILE,
            (str(sync_dir / "test.md"), "test-col", "test.md", 1000.0, 42, now),
        )
        conn.commit()
        conn.close()

        result = _check_sync_health(registry_path)
        assert result.passed is True
        assert "1 collections" in result.message
        assert "oldest sync" in result.message
        assert result.required is False

    def test_stale_sync(self, tmp_path: Path) -> None:
        """Collection with ingested_at > 24h ago triggers warning."""
        from datetime import UTC, datetime, timedelta

        from quarry.sync_registry import open_registry

        registry_path = tmp_path / "registry.db"
        conn = open_registry(registry_path)
        sync_dir = tmp_path / "docs"
        sync_dir.mkdir()
        now = datetime.now(UTC)
        stale_time = (now - timedelta(hours=48)).isoformat()
        conn.execute(
            _INSERT_DIR,
            (str(sync_dir), "stale-col", now.isoformat()),
        )
        conn.execute(
            _INSERT_FILE,
            (str(sync_dir / "old.md"), "stale-col", "old.md", 1000.0, 42, stale_time),
        )
        conn.commit()
        conn.close()

        result = _check_sync_health(registry_path)
        assert result.passed is False
        assert ">24h stale" in result.message
        assert result.required is False

    def test_never_synced(self, tmp_path: Path) -> None:
        """Registration with no files reports never synced."""
        from datetime import UTC, datetime

        from quarry.sync_registry import open_registry

        registry_path = tmp_path / "registry.db"
        conn = open_registry(registry_path)
        sync_dir = tmp_path / "docs"
        sync_dir.mkdir()
        now = datetime.now(UTC).isoformat()
        conn.execute(
            _INSERT_DIR,
            (str(sync_dir), "empty-col", now),
        )
        conn.commit()
        conn.close()

        result = _check_sync_health(registry_path)
        assert result.passed is False
        assert "never synced" in result.message
        assert "empty-col" in result.message
        assert result.required is False


class TestCheckSyncDirectories:
    """Tests for the sync directory existence check."""

    def test_no_registry_file(self, tmp_path: Path) -> None:
        registry_path = tmp_path / "nonexistent" / "registry.db"
        result = _check_sync_directories(registry_path)
        assert result.passed is True
        assert result.required is False
        assert "no registrations" in result.message

    def test_all_directories_exist(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from quarry.sync_registry import open_registry

        registry_path = tmp_path / "registry.db"
        conn = open_registry(registry_path)
        sync_dir = tmp_path / "docs"
        sync_dir.mkdir()
        now = datetime.now(UTC).isoformat()
        conn.execute(
            _INSERT_DIR,
            (str(sync_dir), "good-col", now),
        )
        conn.commit()
        conn.close()

        result = _check_sync_directories(registry_path)
        assert result.passed is True
        assert "1 directories OK" in result.message
        assert result.required is False

    def test_file_at_path_not_treated_as_directory(self, tmp_path: Path) -> None:
        """A regular file at the registered path is not a valid sync directory."""
        from datetime import UTC, datetime

        from quarry.sync_registry import open_registry

        registry_path = tmp_path / "registry.db"
        conn = open_registry(registry_path)
        fake_file = tmp_path / "not-a-dir"
        fake_file.write_text("I am a file")
        now = datetime.now(UTC).isoformat()
        conn.execute(
            _INSERT_DIR,
            (str(fake_file), "file-col", now),
        )
        conn.commit()
        conn.close()

        result = _check_sync_directories(registry_path)
        assert result.passed is False
        assert "1 missing" in result.message
        assert "file-col" in result.message
        assert result.required is False

    def test_missing_directory(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from quarry.sync_registry import open_registry

        registry_path = tmp_path / "registry.db"
        conn = open_registry(registry_path)
        now = datetime.now(UTC).isoformat()
        # Register a directory that doesn't exist
        conn.execute(
            _INSERT_DIR,
            (str(tmp_path / "deleted"), "gone-col", now),
        )
        conn.commit()
        conn.close()

        result = _check_sync_directories(registry_path)
        assert result.passed is False
        assert "1 missing" in result.message
        assert "gone-col" in result.message
        assert result.required is False

    def test_no_registrations(self, tmp_path: Path) -> None:
        from quarry.sync_registry import open_registry

        registry_path = tmp_path / "registry.db"
        conn = open_registry(registry_path)
        conn.close()

        result = _check_sync_directories(registry_path)
        assert result.passed is True
        assert "no registrations" in result.message


def _mock_install_deps(monkeypatch: MP) -> None:
    """Stub out MCP config, mcp-proxy install, and check_environment."""
    import quarry.doctor as doctor_mod

    noop = lambda: doctor_mod.CheckResult(  # noqa: E731
        name="stub", passed=True, message="mocked"
    )
    monkeypatch.setattr(doctor_mod, "_configure_claude_code", noop)
    monkeypatch.setattr(doctor_mod, "_configure_claude_desktop", noop)
    monkeypatch.setattr(doctor_mod, "check_environment", lambda **_kw: 0)
    monkeypatch.setattr("quarry.proxy.install", lambda: "mocked (skipped in test)")


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
        server = config["mcpServers"]["quarry"]
        assert server["command"].endswith("sh")
        assert server["args"][0] == "-c"
        assert "mcp-proxy" in server["args"][1]
        assert "quarry mcp" in server["args"][1]

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
        with patch("quarry.embeddings.download_model_files") as mock_dl:
            mock_dl.return_value = ("/fake/model.onnx", "/fake/tokenizer.json")
            result = run_install()
        assert result == 0
        data_dir = tmp_path / ".punt-labs" / "quarry" / "data" / "default" / "lancedb"
        assert data_dir.is_dir()

    def test_downloads_model(self, tmp_path: Path, monkeypatch: MP):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _mock_install_deps(monkeypatch)
        with patch("quarry.embeddings.download_model_files") as mock_dl:
            mock_dl.return_value = ("/fake/model.onnx", "/fake/tokenizer.json")
            run_install()
        mock_dl.assert_called_once()

    def test_idempotent(self, tmp_path: Path, monkeypatch: MP):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _mock_install_deps(monkeypatch)
        data_dir = tmp_path / ".punt-labs" / "quarry" / "data" / "default" / "lancedb"
        data_dir.mkdir(parents=True)
        with patch("quarry.embeddings.download_model_files") as mock_dl:
            mock_dl.return_value = ("/fake/model.onnx", "/fake/tokenizer.json")
            result = run_install()
        assert result == 0
        assert data_dir.is_dir()

    def test_gpu_failure_marks_error(
        self, tmp_path: Path, monkeypatch: MP, capsys: pytest.CaptureFixture[str]
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _mock_install_deps(monkeypatch)
        with (
            patch(
                "quarry.service.ensure_gpu_runtime",
                return_value="onnxruntime-gpu install failed, CPU restored",
            ),
            patch("quarry.embeddings.download_model_files") as mock_dl,
        ):
            mock_dl.return_value = ("/fake/model.onnx", "/fake/tokenizer.json")
            result = run_install()
        assert result == 1
        captured = capsys.readouterr()
        assert "\u2717" in captured.out
        assert "failed" in captured.out

    def test_model_download_failure_returns_one(self, tmp_path: Path, monkeypatch: MP):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _mock_install_deps(monkeypatch)
        with patch(
            "quarry.embeddings.download_model_files",
            side_effect=RuntimeError("network error"),
        ):
            result = run_install()
        assert result == 1
