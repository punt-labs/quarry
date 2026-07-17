from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from quarry import __version__
from quarry.config import Settings


class TestVersion:
    def test_version_is_string(self):
        assert isinstance(__version__, str)

    def test_version_format(self):
        parts = __version__.split(".")
        assert len(parts) == 3
        assert all(part.isdigit() for part in parts)


class TestSettings:
    def test_defaults(self):
        settings = Settings()
        assert settings.chunk_max_chars == 1800
        assert settings.chunk_overlap_chars == 200
        assert isinstance(settings.lancedb_path, Path)
        expected = (
            Path.home() / ".punt-labs" / "quarry" / "data" / "default" / "registry.db"
        )
        assert settings.registry_path == expected

    def test_override_via_constructor(self):
        settings = Settings(chunk_max_chars=1000)
        assert settings.chunk_max_chars == 1000

    def test_default_lancedb_path_under_home(self):
        settings = Settings()
        home = Path.home()
        expected = home / ".punt-labs" / "quarry" / "data" / "default" / "lancedb"
        assert settings.lancedb_path == expected

    def test_embedding_model_default(self):
        settings = Settings()
        assert settings.embedding_model == "Snowflake/snowflake-arctic-embed-m-v1.5"

    def test_quarry_root_default(self):
        settings = Settings()
        assert settings.quarry_root == Path.home() / ".punt-labs" / "quarry" / "data"


class TestResolveDbPaths:
    def test_default_uses_default_database(self):
        settings = Settings()
        resolved = settings.resolve_db_paths()
        assert resolved.lancedb_path == settings.quarry_root / "default" / "lancedb"
        expected = settings.quarry_root / "default" / "registry.db"
        assert resolved.registry_path == expected

    def test_named_database(self):
        settings = Settings()
        resolved = settings.resolve_db_paths(db_name="work")
        assert resolved.lancedb_path == settings.quarry_root / "work" / "lancedb"
        assert resolved.registry_path == settings.quarry_root / "work" / "registry.db"

    def test_lancedb_path_env_override(self, monkeypatch):
        monkeypatch.setenv("LANCEDB_PATH", "/custom/path")
        settings = Settings()
        resolved = settings.resolve_db_paths(db_name="work")
        assert resolved.lancedb_path == Path("/custom/path")

    def test_does_not_mutate_original(self):
        settings = Settings()
        original_path = settings.lancedb_path
        settings.resolve_db_paths(db_name="other")
        assert settings.lancedb_path == original_path

    def test_rejects_path_separator(self):
        settings = Settings()
        import pytest

        with pytest.raises(ValueError, match="Invalid database name"):
            settings.resolve_db_paths(db_name="../escape")

    def test_rejects_dot_dot(self):
        settings = Settings()
        import pytest

        with pytest.raises(ValueError, match="Invalid database name"):
            settings.resolve_db_paths(db_name="..")


class TestSyncBudget:
    def test_defaults(self):
        settings = Settings()
        assert settings.sync_flush_mb == 32
        assert settings.embed_window_chunks == 512

    def test_sync_flush_mb_zero_rejected(self):
        with pytest.raises(ValidationError, match="greater_than_equal"):
            Settings(sync_flush_mb=0)

    def test_embed_window_chunks_zero_rejected(self):
        with pytest.raises(ValidationError, match="greater_than_equal"):
            Settings(embed_window_chunks=0)

    def test_sync_flush_mb_negative_rejected(self):
        with pytest.raises(ValidationError, match="greater_than_equal"):
            Settings(sync_flush_mb=-1)

    def test_embed_window_chunks_negative_rejected(self):
        with pytest.raises(ValidationError, match="greater_than_equal"):
            Settings(embed_window_chunks=-1)

    def test_one_accepted(self):
        settings = Settings(sync_flush_mb=1, embed_window_chunks=1)
        assert settings.sync_flush_mb == 1
        assert settings.embed_window_chunks == 1


class TestPersistentDb:
    def test_write_and_read(self, tmp_path):
        config_file = tmp_path / "config.toml"
        with patch.object(Settings, "_CONFIG_PATH", config_file):
            Settings.write_default_db("work")
            assert Settings.read_default_db() == "work"

    def test_read_missing_file(self, tmp_path):
        config_file = tmp_path / "nonexistent" / "config.toml"
        with patch.object(Settings, "_CONFIG_PATH", config_file):
            assert Settings.read_default_db() is None

    def test_read_default_returns_none(self, tmp_path):
        config_file = tmp_path / "config.toml"
        with patch.object(Settings, "_CONFIG_PATH", config_file):
            Settings.write_default_db("default")
            assert Settings.read_default_db() is None

    def test_write_creates_parent_dirs(self, tmp_path):
        config_file = tmp_path / "nested" / "dir" / "config.toml"
        with patch.object(Settings, "_CONFIG_PATH", config_file):
            Settings.write_default_db("coding")
            assert config_file.exists()


class TestActiveDb:
    """The process-scoped --db override the client tier reads for run dirs."""

    def test_no_override_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        Settings.set_active_db("")
        monkeypatch.setattr(Settings, "read_default_db", classmethod(lambda _cls: None))
        assert Settings.active_db() is None

    def test_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            Settings, "read_default_db", classmethod(lambda _cls: "persisted")
        )
        Settings.set_active_db("work")
        try:
            assert Settings.active_db() == "work"
        finally:
            Settings.set_active_db("")

    def test_empty_override_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            Settings, "read_default_db", classmethod(lambda _cls: "persisted")
        )
        Settings.set_active_db("")
        assert Settings.active_db() == "persisted"
