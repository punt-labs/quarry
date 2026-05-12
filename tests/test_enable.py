"""Tests for the enable/disable module -- T1 through T14."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quarry.enable import (
    _CLAUDEMD_BEGIN,
    _CLAUDEMD_BLOCK,
    _CLAUDEMD_END,
    _CONFIG_TEMPLATE,
    DisableResult,
    EnableResult,
    _append_claudemd_block,
    _bootstrap_ethos_memory,
    _remove_claudemd_block,
    _write_project_config,
    disable_project,
    enable_project,
)
from quarry.sync_registry import (
    list_registrations,
    open_registry,
    register_directory,
)

# -----------------------------------------------------------------------
# T1: enable registers a new directory
# -----------------------------------------------------------------------


class TestT1EnableNewDirectory:
    def test_registers_new_directory(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        # Ensure registry exists.
        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            result = enable_project(project)

        assert isinstance(result, EnableResult)
        assert result.created_registration is True
        assert result.collection == "myproject"
        assert result.directory == str(project)

        # Verify registration in the registry.
        conn = open_registry(settings.registry_path)
        regs = list_registrations(conn)
        conn.close()
        assert len(regs) == 1
        assert regs[0].collection == "myproject"


# -----------------------------------------------------------------------
# T2: enable is idempotent on already-registered directory
# -----------------------------------------------------------------------


class TestT2EnableIdempotent:
    def test_idempotent_on_registered_directory(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        # Pre-register.
        conn = open_registry(settings.registry_path)
        register_directory(conn, project, "foo")
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            result = enable_project(project)

        assert result.collection == "foo"
        assert result.created_registration is False


# -----------------------------------------------------------------------
# T3: enable on child of registered parent raises ValueError
# -----------------------------------------------------------------------


class TestT3EnableChildRaisesValueError:
    def test_child_of_registered_parent_raises(self, tmp_path: Path) -> None:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        register_directory(conn, parent, "project")
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
            pytest.raises(ValueError, match="already covered by the registration at"),
        ):
            enable_project(child)


# -----------------------------------------------------------------------
# T4: enable with --collection override
# -----------------------------------------------------------------------


class TestT4EnableCollectionOverride:
    def test_collection_override(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            result = enable_project(project, collection_override="custom")

        assert result.collection == "custom"
        assert result.created_registration is True


# -----------------------------------------------------------------------
# T5: enable creates config file
# -----------------------------------------------------------------------


class TestT5EnableCreatesConfig:
    def test_creates_config_file(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            result = enable_project(project)

        config_path = project / ".punt-labs" / "quarry" / "config.md"
        assert config_path.exists()
        content = config_path.read_text()
        assert "auto_capture:" in content
        assert result.config_path == str(config_path)


# -----------------------------------------------------------------------
# T6: enable does not overwrite existing config file
# -----------------------------------------------------------------------


class TestT6EnablePreservesExistingConfig:
    def test_does_not_overwrite_existing_config(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        # Create a custom config before enable.
        config_dir = project / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "config.md"
        custom_content = "---\ncustom: true\n---\n"
        config_path.write_text(custom_content)

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            enable_project(project)

        assert config_path.read_text() == custom_content


# -----------------------------------------------------------------------
# T7: enable creates ethos ext quarry.yaml files
# -----------------------------------------------------------------------


class TestT7EnableCreatesEthosExtFiles:
    def test_creates_quarry_yaml_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identities_dir = tmp_path / "identities"
        identities_dir.mkdir()

        # Create minimal identity YAML files.
        (identities_dir / "claude.yaml").write_text("agent: claude\n")
        (identities_dir / "rmh.yaml").write_text("agent: rmh\n")

        monkeypatch.setattr("quarry.enable._GLOBAL_IDENTITIES", identities_dir)

        created, updated, already_set, skipped = _bootstrap_ethos_memory()

        assert skipped is False
        assert "claude" in created
        assert "rmh" in created
        # session_context written on freshly created files yields "updated".
        assert set(updated) == {"claude", "rmh"}
        assert already_set == []

        # Check files were created.
        claude_yaml = identities_dir / "claude.ext" / "quarry.yaml"
        rmh_yaml = identities_dir / "rmh.ext" / "quarry.yaml"
        assert claude_yaml.exists()
        assert rmh_yaml.exists()
        assert "memory_collection: memory-claude" in claude_yaml.read_text()
        assert "memory_collection: memory-rmh" in rmh_yaml.read_text()


# -----------------------------------------------------------------------
# T7b: existing quarry.yaml with wrong memory_collection is not modified
# -----------------------------------------------------------------------


class TestT7bExistingQuarryYamlNotModified:
    def test_wrong_memory_collection_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identities_dir = tmp_path / "identities"
        identities_dir.mkdir()

        (identities_dir / "claude.yaml").write_text("agent: claude\n")

        # Pre-create ext dir with wrong memory_collection.
        ext_dir = identities_dir / "claude.ext"
        ext_dir.mkdir()
        quarry_yaml = ext_dir / "quarry.yaml"
        quarry_yaml.write_text("memory_collection: wrong-name\n")

        monkeypatch.setattr("quarry.enable._GLOBAL_IDENTITIES", identities_dir)

        created, _, _, skipped = _bootstrap_ethos_memory()

        assert skipped is False
        assert "claude" not in created  # Not in created since file already existed.
        # The wrong value should be preserved.
        assert "memory_collection: wrong-name" in quarry_yaml.read_text()


# -----------------------------------------------------------------------
# T8: enable skips ethos when identities dir missing
# -----------------------------------------------------------------------


class TestT8EnableSkipsEthosWhenMissing:
    def test_skips_when_identities_dir_missing(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch(
                "quarry.enable._GLOBAL_IDENTITIES",
                tmp_path / "nonexistent-identities",
            ),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            result = enable_project(project)

        assert result.ethos_skipped is True


# -----------------------------------------------------------------------
# T9: enable derives captures collection name correctly
# -----------------------------------------------------------------------


class TestT9EnableCapturesCollectionName:
    def test_captures_collection_name(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            result = enable_project(project)

        assert result.captures_collection == f"{result.collection}-captures"


# -----------------------------------------------------------------------
# T10: disable removes registration
# -----------------------------------------------------------------------


class TestT10DisableRemovesRegistration:
    def test_removes_registration(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        # Enable first.
        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            enable_result = enable_project(project)
            disable_result = disable_project(project)

        assert isinstance(disable_result, DisableResult)
        assert disable_result.collection == enable_result.collection

        # Verify registration is gone.
        conn = open_registry(settings.registry_path)
        regs = list_registrations(conn)
        conn.close()
        assert len(regs) == 0


# -----------------------------------------------------------------------
# T11: disable removes config file
# -----------------------------------------------------------------------


class TestT11DisableRemovesConfig:
    def test_removes_config_file(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            enable_project(project)
            config_path = project / ".punt-labs" / "quarry" / "config.md"
            assert config_path.exists()

            result = disable_project(project)

        assert result.config_removed is True
        assert not config_path.exists()


# -----------------------------------------------------------------------
# T12: disable with --keep-data preserves LanceDB data
# -----------------------------------------------------------------------


class TestT12DisableKeepData:
    def test_keep_data_preserves_chunks(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
            patch(
                "quarry.database.delete_collection",
            ) as mock_delete,
        ):
            enable_project(project)
            result = disable_project(project, keep_data=True)

        # delete_collection should NOT have been called.
        mock_delete.assert_not_called()
        assert result.deleted_chunks == 0


# -----------------------------------------------------------------------
# T13: disable preserves agent memory collections
# -----------------------------------------------------------------------


class TestT13DisablePreservesAgentMemory:
    def test_preserves_memory_collections(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
            patch(
                "quarry.database.delete_collection",
            ) as mock_delete,
        ):
            enable_project(project)
            disable_project(project, keep_data=False)

        # delete_collection should only be called for project + captures,
        # never for memory-* collections.
        deleted_collections = [call.args[1] for call in mock_delete.call_args_list]
        assert all(not c.startswith("memory-") for c in deleted_collections), (
            f"Memory collection deleted: {deleted_collections}"
        )
        # Should be called exactly twice: once for project, once for captures.
        assert mock_delete.call_count == 2


# -----------------------------------------------------------------------
# T14: disable on unregistered directory returns error
# -----------------------------------------------------------------------


class TestT14DisableUnregisteredRaises:
    def test_raises_on_unregistered_directory(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        # Ensure empty registry.
        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
            pytest.raises(ValueError, match="no registration covers"),
        ):
            disable_project(project)


# -----------------------------------------------------------------------
# _write_project_config direct tests
# -----------------------------------------------------------------------


class TestWriteProjectConfig:
    def test_creates_config_with_template(self, tmp_path: Path) -> None:
        result_path = _write_project_config(tmp_path)
        config = Path(result_path)
        assert config.exists()
        assert config.read_text() == _CONFIG_TEMPLATE

    def test_idempotent_no_overwrite(self, tmp_path: Path) -> None:
        _write_project_config(tmp_path)
        config = tmp_path / ".punt-labs" / "quarry" / "config.md"
        config.write_text("custom content")
        _write_project_config(tmp_path)
        assert config.read_text() == "custom content"

    def test_atomic_no_overwrite_existing(self, tmp_path: Path) -> None:
        """Verify O_CREAT|O_EXCL path: pre-existing file is never opened for write."""
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "config.md"
        original = "do not touch\n"
        config_path.write_text(original)

        _write_project_config(tmp_path)

        assert config_path.read_text() == original

    def test_fd_closed_when_fdopen_raises(self, tmp_path: Path) -> None:
        """Verify fd is closed if os.fdopen raises before taking ownership."""
        import os as _os

        real_open = _os.open

        captured_fd: list[int] = []

        def tracking_open(path: str, flags: int, mode: int = 0o777) -> int:
            fd = real_open(path, flags, mode)
            captured_fd.append(fd)
            return fd

        with (
            patch("quarry.enable.os.open", side_effect=tracking_open),
            patch("quarry.enable.os.fdopen", side_effect=OSError("fdopen failed")),
            patch("quarry.enable.os.close") as mock_close,
            pytest.raises(OSError, match="fdopen failed"),
        ):
            _write_project_config(tmp_path)

        assert len(captured_fd) == 1
        mock_close.assert_called_once_with(captured_fd[0])


# -----------------------------------------------------------------------
# T15: disable on child of registered parent raises (not silent deletion)
# -----------------------------------------------------------------------


class TestT15DisableOnChildOfRegisteredParentRaises:
    def test_disable_on_child_of_registered_parent_raises(self, tmp_path: Path) -> None:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        register_directory(conn, parent, "project")
        conn.close()

        with (
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
            pytest.raises(ValueError, match="covered by parent registration"),
        ):
            disable_project(child)

        # Verify parent registration was NOT deleted.
        conn = open_registry(settings.registry_path)
        regs = list_registrations(conn)
        conn.close()
        assert len(regs) == 1
        assert regs[0].collection == "project"


# -----------------------------------------------------------------------
# T16: _bootstrap_ethos_memory skips bad YAML without crashing
# -----------------------------------------------------------------------


class TestT16BootstrapEthosMemorySkipsBadYaml:
    def test_skips_bad_yaml_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identities_dir = tmp_path / "identities"
        identities_dir.mkdir()

        # Valid identity.
        (identities_dir / "alice.yaml").write_text("agent: alice\n")
        # Malformed identity — will cause _write_ethos_ext_session_context to
        # raise when it tries to yaml.safe_load the quarry.yaml we create for
        # it (the quarry.yaml itself is valid, so we need to make the ext call
        # raise). We simulate this by making the ext call raise for "bad".
        (identities_dir / "bad.yaml").write_text("agent: bad\n")

        monkeypatch.setattr("quarry.enable._GLOBAL_IDENTITIES", identities_dir)

        # Make the session context writer raise for "bad" handle only.
        original_write = None
        try:
            from quarry.doctor import (
                _write_ethos_ext_session_context as _orig,
            )

            original_write = _orig
        except ImportError:
            pass

        def selective_raise(quarry_yaml: Path, handle: str) -> str:
            if handle == "bad":
                msg = "simulated YAML parse failure"
                raise ValueError(msg)
            assert original_write is not None
            return original_write(quarry_yaml, handle)

        monkeypatch.setattr(
            "quarry.doctor._write_ethos_ext_session_context",
            selective_raise,
        )

        created, updated, already_set, skipped = _bootstrap_ethos_memory()

        assert skipped is False
        # alice was processed.
        assert "alice" in created
        # bad was also created (quarry.yaml file written) but the session
        # context call failed — it should not appear in updated/already_set.
        assert "bad" in created
        assert "bad" not in updated
        assert "bad" not in already_set

        # alice's ext file exists.
        assert (identities_dir / "alice.ext" / "quarry.yaml").exists()
        # bad's ext file also exists (file was created before the call failed).
        assert (identities_dir / "bad.ext" / "quarry.yaml").exists()


# -----------------------------------------------------------------------
# T17: enable with collection override on child of registered parent raises
# -----------------------------------------------------------------------


class TestT17EnableWithOverrideOnChildRaises:
    def test_override_does_not_bypass_parent_check(self, tmp_path: Path) -> None:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        register_directory(conn, parent, "project")
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
            pytest.raises(ValueError, match="already covered by the registration"),
        ):
            enable_project(child, collection_override="custom")


# -----------------------------------------------------------------------
# T18: enable resolves relative paths
# -----------------------------------------------------------------------


class TestT18EnableResolvesRelativePath:
    def test_enable_with_relative_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        monkeypatch.chdir(project)

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            result = enable_project(Path("."))  # noqa: PTH201

        # Directory should be the resolved absolute path, not ".".
        assert result.directory == str(project)
        assert result.created_registration is True


# -----------------------------------------------------------------------
# T19: disable resolves relative paths
# -----------------------------------------------------------------------


class TestT19DisableResolvesRelativePath:
    def test_disable_with_relative_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        monkeypatch.chdir(project)

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            enable_project(project)
            result = disable_project(Path("."))  # noqa: PTH201

        assert result.directory == str(project)


# -----------------------------------------------------------------------
# T20: _check_enable_status returns passed=False when config.md missing
# -----------------------------------------------------------------------


class TestT20CheckEnableStatusConfigMissing:
    def test_config_missing_returns_not_passed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from quarry.doctor import _check_enable_status

        project = tmp_path / "myproject"
        project.mkdir()
        monkeypatch.chdir(project)

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        register_directory(conn, project, "myproject")
        conn.close()

        # No config.md on disk — only the registration exists.
        with (
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            result = _check_enable_status(settings.registry_path, str(project))

        assert result.passed is False
        assert "config.md missing" in result.message
        assert result.required is False

    def test_config_present_returns_passed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from quarry.doctor import _check_enable_status

        project = tmp_path / "myproject"
        project.mkdir()
        monkeypatch.chdir(project)

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        register_directory(conn, project, "myproject")
        conn.close()

        # Create config.md so it's present.
        config_dir = project / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  session_sync: true\n---\n"
        )

        with (
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            result = _check_enable_status(settings.registry_path, str(project))

        assert result.passed is True
        assert "config.md missing" not in result.message


# -----------------------------------------------------------------------
# CLAUDE.md block injection tests
# -----------------------------------------------------------------------


class TestEnableAppendsClaudemdBlock:
    def test_enable_creates_claudemd_with_markers(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch("quarry.config.resolve_db_paths", return_value=settings),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            result = enable_project(project)

        assert result.claudemd_appended is True
        claudemd = project / "CLAUDE.md"
        assert claudemd.exists()
        content = claudemd.read_text()
        assert _CLAUDEMD_BEGIN in content
        assert _CLAUDEMD_END in content
        assert "Local semantic search is available via quarry." in content


class TestEnableClaudemdIdempotent:
    def test_running_enable_twice_does_not_duplicate(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch("quarry.config.resolve_db_paths", return_value=settings),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            result1 = enable_project(project)
            result2 = enable_project(project)

        assert result1.claudemd_appended is True
        assert result2.claudemd_appended is False
        content = (project / "CLAUDE.md").read_text()
        assert content.count(_CLAUDEMD_BEGIN) == 1


class TestEnableAppendsToExistingClaudemd:
    def test_existing_content_preserved(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        claudemd = project / "CLAUDE.md"
        claudemd.write_text("# My Project\n\nExisting content.\n")

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch("quarry.config.resolve_db_paths", return_value=settings),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            result = enable_project(project)

        assert result.claudemd_appended is True
        content = claudemd.read_text()
        assert content.startswith("# My Project\n\nExisting content.\n")
        assert _CLAUDEMD_BEGIN in content
        assert _CLAUDEMD_END in content


class TestDisableRemovesClaudemdBlock:
    def test_disable_removes_markers_and_content(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch("quarry.config.resolve_db_paths", return_value=settings),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            enable_project(project)
            result = disable_project(project)

        assert result.claudemd_removed is True
        claudemd = project / "CLAUDE.md"
        assert claudemd.exists()
        content = claudemd.read_text()
        assert _CLAUDEMD_BEGIN not in content
        assert _CLAUDEMD_END not in content


class TestDisablePreservesOtherClaudemdContent:
    def test_other_content_survives(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        claudemd = project / "CLAUDE.md"
        claudemd.write_text("# My Project\n\nKeep this.\n")

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = open_registry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
            patch("quarry.config.resolve_db_paths", return_value=settings),
            patch("quarry.config.load_settings", return_value=MagicMock()),
        ):
            enable_project(project)
            result = disable_project(project)

        assert result.claudemd_removed is True
        content = claudemd.read_text()
        assert "# My Project" in content
        assert "Keep this." in content
        assert _CLAUDEMD_BEGIN not in content


class TestDisableNoopWhenNoMarkers:
    def test_no_markers_no_change(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        claudemd = project / "CLAUDE.md"
        original = "# Untouched\n"
        claudemd.write_text(original)

        removed = _remove_claudemd_block(project)

        assert removed is False
        assert claudemd.read_text() == original


class TestDisableNoopWhenNoClaudemd:
    def test_missing_file_no_error(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        removed = _remove_claudemd_block(project)

        assert removed is False


class TestAppendClaudemdBlockDirect:
    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        appended = _append_claudemd_block(tmp_path)

        assert appended is True
        claudemd = tmp_path / "CLAUDE.md"
        assert claudemd.exists()
        content = claudemd.read_text()
        assert content == _CLAUDEMD_BLOCK

    def test_appends_newline_to_file_without_trailing_newline(
        self, tmp_path: Path
    ) -> None:
        claudemd = tmp_path / "CLAUDE.md"
        claudemd.write_text("no trailing newline")

        appended = _append_claudemd_block(tmp_path)

        assert appended is True
        content = claudemd.read_text()
        assert content.startswith("no trailing newline\n")
        assert _CLAUDEMD_BEGIN in content
