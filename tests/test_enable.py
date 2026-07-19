"""Tests for the enable/disable module.

enable/disable drive the daemon's registry through a client port
(``RegistryClient``); these tests supply the in-memory ``FakeRegistryClient``
(from conftest) so no real ``SyncRegistry`` or daemon is involved.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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
from tests.conftest import FakeRegistryClient

_NO_ETHOS = "quarry.enable._GLOBAL_IDENTITIES"


class TestT1EnableNewDirectory:
    def test_registers_new_directory(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result = enable_project(project, client)

        assert isinstance(result, EnableResult)
        assert result.created_registration is True
        assert result.collection == "myproject"
        assert result.directory == str(project)
        assert [r.collection for r in client.registered] == ["myproject"]
        assert client.registered[0].directory == str(project)


class TestT2EnableIdempotent:
    def test_idempotent_on_registered_directory(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient([("foo", project)])

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result = enable_project(project, client)

        assert result.collection == "foo"
        assert result.created_registration is False
        assert client.registered == []


class TestT3EnableChildRaisesValueError:
    def test_child_of_registered_parent_raises(self, tmp_path: Path) -> None:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()
        client = FakeRegistryClient([("project", parent)])

        with (
            patch(_NO_ETHOS, tmp_path / "no-ethos"),
            pytest.raises(ValueError, match="already covered by the registration at"),
        ):
            enable_project(child, client)


class TestT4EnableCollectionOverride:
    def test_collection_override(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result = enable_project(project, client, collection_override="custom")

        assert result.collection == "custom"
        assert result.created_registration is True
        assert client.registered[0].collection == "custom"


class TestT5EnableCreatesConfig:
    def test_creates_config_file(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result = enable_project(project, client)

        config_path = project / ".punt-labs" / "quarry" / "config.md"
        assert config_path.exists()
        assert "auto_capture:" in config_path.read_text()
        assert result.config_path == str(config_path)


class TestT6EnablePreservesExistingConfig:
    def test_does_not_overwrite_existing_config(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        config_dir = project / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "config.md"
        custom_content = "---\ncustom: true\n---\n"
        config_path.write_text(custom_content)
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_project(project, client)

        assert config_path.read_text() == custom_content


class TestT7EnableCreatesEthosExtFiles:
    def test_creates_quarry_yaml_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identities_dir = tmp_path / "identities"
        identities_dir.mkdir()

        (identities_dir / "claude.yaml").write_text("agent: claude\n")
        (identities_dir / "rmh.yaml").write_text("agent: rmh\n")

        monkeypatch.setattr("quarry.enable._GLOBAL_IDENTITIES", identities_dir)

        created, updated, already_set, failed, skipped = _bootstrap_ethos_memory()

        assert skipped is False
        assert failed == []
        assert "claude" in created
        assert "rmh" in created
        assert set(updated) == {"claude", "rmh"}
        assert already_set == []

        claude_yaml = identities_dir / "claude.ext" / "quarry.yaml"
        rmh_yaml = identities_dir / "rmh.ext" / "quarry.yaml"
        assert claude_yaml.exists()
        assert rmh_yaml.exists()
        assert "memory_collection: memory-claude" in claude_yaml.read_text()
        assert "memory_collection: memory-rmh" in rmh_yaml.read_text()


class TestT7bExistingQuarryYamlNotModified:
    def test_wrong_memory_collection_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identities_dir = tmp_path / "identities"
        identities_dir.mkdir()

        (identities_dir / "claude.yaml").write_text("agent: claude\n")

        ext_dir = identities_dir / "claude.ext"
        ext_dir.mkdir()
        quarry_yaml = ext_dir / "quarry.yaml"
        quarry_yaml.write_text("memory_collection: wrong-name\n")

        monkeypatch.setattr("quarry.enable._GLOBAL_IDENTITIES", identities_dir)

        created, _, _, _, skipped = _bootstrap_ethos_memory()

        assert skipped is False
        assert "claude" not in created
        assert "memory_collection: wrong-name" in quarry_yaml.read_text()


class TestT8EnableSkipsEthosWhenMissing:
    def test_skips_when_identities_dir_missing(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "nonexistent-identities"):
            result = enable_project(project, client)

        assert result.ethos_skipped is True


class TestT9EnableCapturesCollectionName:
    def test_captures_collection_name(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result = enable_project(project, client)

        assert result.captures_collection == f"{result.collection}-captures"


class TestT10DisableRemovesRegistration:
    def test_removes_registration(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_result = enable_project(project, client)
            disable_result = disable_project(project, client)

        assert isinstance(disable_result, DisableResult)
        assert disable_result.collection == enable_result.collection
        assert [r.collection for r in client.deregistered] == ["myproject"]
        assert client.collections == []


class TestT11DisableRemovesConfig:
    def test_removes_config_file(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_project(project, client)
            config_path = project / ".punt-labs" / "quarry" / "config.md"
            assert config_path.exists()

            result = disable_project(project, client)

        assert result.config_removed is True
        assert not config_path.exists()


class TestT12DisableKeepData:
    def test_keep_data_dispatches_no_captures_purge(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_project(project, client)
            result = disable_project(project, client, keep_data=True)

        # keep_data suppresses the captures purge: the client dispatches a
        # deregister with keep_data=True and no delete_collection.
        assert result.removed >= 0
        assert client.deregistered[0].keep_data is True
        assert client.deleted == []


class TestT13DisablePurgesCapturesSibling:
    def test_purges_only_captures_never_memory(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_project(project, client)
            disable_project(project, client, keep_data=False)

        # The daemon purges the main collection via deregister; the client purges
        # exactly the -captures sibling — never a memory-* collection.
        assert client.deregistered[0].collection == "myproject"
        assert client.deregistered[0].keep_data is False
        assert client.deleted == ["myproject-captures"]
        assert all(not name.startswith("memory-") for name in client.deleted)


class TestT14DisableUnregisteredIsIdempotentNoop:
    def test_unregistered_directory_is_noop_success(self, tmp_path: Path) -> None:
        # Disabling a never-enabled directory is not an error — it is an idempotent
        # no-op: no deregister, empty collection, exit-0 result.
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        result = disable_project(project, client)

        assert result.collection == ""
        assert result.removed == 0
        assert client.deregistered == []
        assert client.deleted == []


class TestDisableIdempotentRetrySafe:
    def test_already_deregistered_still_cleans_local_files(
        self, tmp_path: Path
    ) -> None:
        # A prior partial disable removed the registration but left the local
        # files. A retry (covering is None) must still clean them and succeed.
        project = tmp_path / "myproject"
        project.mkdir()
        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_project(project, FakeRegistryClient())
        config_path = project / ".punt-labs" / "quarry" / "config.md"
        assert config_path.exists()

        # Fresh client with NO registrations models the already-deregistered state.
        result = disable_project(project, FakeRegistryClient())

        assert result.collection == ""
        assert result.config_removed is True
        assert not config_path.exists()

    def test_rejected_captures_purge_warns_but_disable_succeeds(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A rejected captures purge is best-effort: the primary teardown
        # (deregister + local file cleanup) succeeded, so disable warns and STILL
        # returns success — it does not fail the whole command or leave the project
        # files claiming enabled.
        from quarry.client import QuarryError

        project = tmp_path / "myproject"
        project.mkdir()
        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_project(project, FakeRegistryClient())
        config_path = project / ".punt-labs" / "quarry" / "config.md"

        failing = FakeRegistryClient(
            [("myproject", project)],
            delete_error=QuarryError("captures purge rejected"),
        )
        with caplog.at_level("WARNING", logger="quarry.enable"):
            result = disable_project(project, failing)

        # Disable succeeded: registration dropped, local files cleaned.
        assert result.collection == "myproject"
        assert failing.deregistered[0].collection == "myproject"
        assert not config_path.exists()
        # The rejected purge was caught (not recorded) and surfaced as a warning.
        assert failing.deleted == []
        assert "captures purge for myproject-captures was rejected" in caplog.text


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


class TestT15DisableOnChildOfRegisteredParentRaises:
    def test_disable_on_child_of_registered_parent_raises(self, tmp_path: Path) -> None:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()
        client = FakeRegistryClient([("project", parent)])

        with pytest.raises(ValueError, match="covered by parent registration"):
            disable_project(child, client)

        # The parent registration must NOT be deregistered.
        assert client.deregistered == []
        assert client.collections == ["project"]


class TestT16BootstrapEthosMemorySkipsBadYaml:
    def test_skips_bad_yaml_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identities_dir = tmp_path / "identities"
        identities_dir.mkdir()

        (identities_dir / "alice.yaml").write_text("agent: alice\n")
        (identities_dir / "bad.yaml").write_text("agent: bad\n")

        monkeypatch.setattr("quarry.enable._GLOBAL_IDENTITIES", identities_dir)

        from yaml import YAMLError

        from quarry.doctor import _write_ethos_ext_session_context as original_write

        def selective_raise(quarry_yaml: Path, handle: str) -> str:
            if handle == "bad":
                msg = "simulated YAML parse failure"
                raise YAMLError(msg)
            return original_write(quarry_yaml, handle)

        monkeypatch.setattr(
            "quarry.doctor._write_ethos_ext_session_context",
            selective_raise,
        )

        created, updated, already_set, failed, skipped = _bootstrap_ethos_memory()

        assert skipped is False
        assert "alice" in created
        # bad's quarry.yaml file was written (so it's "created"), but the
        # session_context write raised — it lands in failed, never updated.
        assert "bad" in created
        assert "bad" in failed
        assert "bad" not in updated
        assert "bad" not in already_set

        assert (identities_dir / "alice.ext" / "quarry.yaml").exists()
        assert (identities_dir / "bad.ext" / "quarry.yaml").exists()

    def test_non_utf8_identity_file_recorded_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-UTF8/corrupt ext quarry.yaml makes the session-context reader raise
        # UnicodeDecodeError (a ValueError, not OSError). enable must record the
        # handle in ethos_failed and continue, never crash.
        identities_dir = tmp_path / "identities"
        identities_dir.mkdir()
        (identities_dir / "alice.yaml").write_text("agent: alice\n")
        ext_dir = identities_dir / "alice.ext"
        ext_dir.mkdir()
        (ext_dir / "quarry.yaml").write_bytes(b"memory_collection: \xff\xfe bad\n")
        monkeypatch.setattr("quarry.enable._GLOBAL_IDENTITIES", identities_dir)

        _created, updated, already_set, failed, skipped = _bootstrap_ethos_memory()

        assert skipped is False
        assert "alice" in failed
        assert "alice" not in updated
        assert "alice" not in already_set


class TestT17EnableWithOverrideOnChildRaises:
    def test_override_does_not_bypass_parent_check(self, tmp_path: Path) -> None:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()
        client = FakeRegistryClient([("project", parent)])

        with (
            patch(_NO_ETHOS, tmp_path / "no-ethos"),
            pytest.raises(ValueError, match="already covered by the registration"),
        ):
            enable_project(child, client, collection_override="custom")


class TestT18EnableResolvesRelativePath:
    def test_enable_with_relative_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        monkeypatch.chdir(project)
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result = enable_project(Path(), client)

        assert result.directory == str(project)
        assert result.created_registration is True


class TestT19DisableResolvesRelativePath:
    def test_disable_with_relative_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        monkeypatch.chdir(project)
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_project(project, client)
            result = disable_project(Path(), client)

        assert result.directory == str(project)


class TestT20CheckEnableStatusConfigMissing:
    # enable-status is computed from the sync registry (the cwd's registered
    # collection) plus local config.md presence.  A registered cwd with no
    # config.md fails; with config.md it passes.
    @staticmethod
    def _register(registry_path: Path, project: Path) -> None:
        from quarry.sync_registry import SyncRegistry

        conn = SyncRegistry(registry_path)
        conn.register_directory(project, "myproject")
        conn.close()

    def test_config_missing_returns_not_passed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from quarry.doctor import _check_enable_status

        project = tmp_path / "myproject"
        project.mkdir()
        monkeypatch.chdir(project)
        registry_path = tmp_path / "registry.db"
        self._register(registry_path, project)

        result = _check_enable_status(registry_path, str(project))

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
        registry_path = tmp_path / "registry.db"
        self._register(registry_path, project)

        config_dir = project / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  session_sync: true\n---\n"
        )

        result = _check_enable_status(registry_path, str(project))

        assert result.passed is True
        assert "config.md missing" not in result.message


class TestEnableAppendsClaudemdBlock:
    def test_enable_creates_claudemd_with_markers(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result = enable_project(project, client)

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
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result1 = enable_project(project, client)
            result2 = enable_project(project, client)

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
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result = enable_project(project, client)

        assert result.claudemd_appended is True
        content = claudemd.read_text()
        assert content.startswith("# My Project\n\nExisting content.\n")
        assert _CLAUDEMD_BEGIN in content
        assert _CLAUDEMD_END in content


class TestDisableRemovesClaudemdBlock:
    def test_disable_removes_markers_and_content(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_project(project, client)
            result = disable_project(project, client)

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
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_project(project, client)
            result = disable_project(project, client)

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
        assert _CLAUDEMD_BEGIN in content
