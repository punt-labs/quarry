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
    _CONFIG_TEMPLATE,
    DisableResult,
    EnableResult,
    _write_project_config,
    disable_project,
    enable_project,
)
from quarry.enabled_marker import EnabledMarker
from quarry.gitignore import CAPTURES_GITIGNORE_ENTRY
from quarry.guidance import REPO_IMPORT_LINE
from tests.conftest import FakeRegistryClient

_NO_ETHOS = "quarry.ethos_memory._GLOBAL_IDENTITIES"


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


class TestKeepDataArchiveNoMerge:
    """A keep-data archive is never silently inherited by an unrelated project."""

    def test_unrelated_same_leaf_gets_distinct_collection(self, tmp_path: Path) -> None:
        """enable dir_a "backend" → keep-data disable → enable an unrelated dir_b.

        dir_b's leaf name is also "backend", but "backend" is archived (retained)
        from dir_a's keep-data disable.  The name-picker must NOT hand dir_b the
        archived "backend" (which would merge dir_a's kept chunks into dir_b's
        collection) — it gets a distinct name, so the two projects never share a
        collection.
        """
        dir_a = tmp_path / "work" / "backend"
        dir_a.mkdir(parents=True)
        dir_b = tmp_path / "other" / "backend"
        dir_b.mkdir(parents=True)
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_project(dir_a, client)
            assert client.registered[-1].collection == "backend"
            disable_project(dir_a, client, keep_data=True)
            result = enable_project(dir_b, client)

        assert result.collection == "backend-other"  # distinct, disambiguated
        assert result.collection != "backend"  # NOT the archived collection
        assert client.registered[-1].collection == "backend-other"  # no merge

    def test_same_dir_reenable_readopts_same_collection(self, tmp_path: Path) -> None:
        """Re-enabling the SAME dir re-adopts its archived collection name.

        enable dir_a "backend" → keep-data disable (archived under original
        directory dir_a) → re-enable dir_a.  Because dir_a owns the archive, the
        re-enable must re-adopt the SAME "backend" collection (reusing its kept
        chunks), not pick a disambiguated fresh name.  A different directory is
        the ``test_unrelated_same_leaf`` case; this is the intended keep-data
        round-trip.
        """
        dir_a = tmp_path / "work" / "backend"
        dir_a.mkdir(parents=True)
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_project(dir_a, client)
            disable_project(dir_a, client, keep_data=True)
            result = enable_project(dir_a, client)

        assert result.collection == "backend"  # re-adopt, not "backend-work"
        assert client.registered[-1].collection == "backend"

    def test_unrelated_dir_with_distinct_leaf_keeps_identity(
        self, tmp_path: Path
    ) -> None:
        """An unrelated dir with its OWN leaf name is untouched by another's archive.

        enable dir_a "backend" → keep-data disable (archived) → enable dir_b, whose
        leaf is the distinct "svc".  dir_a's archive belongs to dir_a; it neither
        attracts nor renames an unrelated directory that was never going to collide
        — dir_b simply registers under its own leaf "svc".  (The refusal to *merge*
        onto another dir's archived name lives in ``SyncRegistry.register_directory``
        and is exercised in test_registry.)
        """
        dir_a = tmp_path / "work" / "backend"
        dir_a.mkdir(parents=True)
        dir_b = tmp_path / "other" / "svc"
        dir_b.mkdir(parents=True)
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_project(dir_a, client)
            disable_project(dir_a, client, keep_data=True)
            result = enable_project(dir_b, client)

        assert result.collection == "svc"  # dir_b keeps its own identity

    def test_unrelated_dir_avoids_chunk_bearing_name(self, tmp_path: Path) -> None:
        """A different dir never claims a name that already holds chunks.

        A collection "backend" already holds chunks on the daemon (a captures/
        memory/remember target, or a subsumed-then-evicted child) but has NO
        registration.  Enabling an unrelated dir_b whose leaf is also "backend"
        must NOT be handed that chunk-bearing name — the picker avoids every
        chunk-bearing collection reported on the wire, so it disambiguates and the
        two projects never share a collection.  FAILS against a live-plus-retained
        -only picker (which would return "backend"); passes once chunk_collections
        joins the avoid-set.
        """
        dir_b = tmp_path / "other" / "backend"
        dir_b.mkdir(parents=True)
        client = FakeRegistryClient(chunk_collections=["backend"])

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result = enable_project(dir_b, client)

        assert result.collection == "backend-other"  # disambiguated, no merge
        assert result.collection != "backend"
        assert client.registered[-1].collection == "backend-other"


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


class TestEnableRefreshesVendoredEthosTree:
    """``quarry enable`` refreshes the repo's committed ext files — never creates."""

    _V1_BLOCK = "\nsession_context: |\n  ## Memory\n  \n  old guide\n"

    def test_reports_refreshed_vendored_identities(self, unpinned_root: Path) -> None:
        identities = unpinned_root / ".punt-labs" / "ethos" / "identities"
        for handle in ("claude", "rmh"):
            ext = identities / f"{handle}.ext"
            ext.mkdir(parents=True)
            (ext / "quarry.yaml").write_text(
                f"memory_collection: memory-{handle}\n{self._V1_BLOCK}"
            )
        (identities / "kpz.yaml").write_text("agent: kpz\n")  # no ext: stays without
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, unpinned_root / "no-ethos"):
            result = enable_project(unpinned_root, client)

        assert result.ethos.vendored_updated == ["claude", "rmh"]
        assert (
            "## Memory (quarry guide v2)"
            in (identities / "rmh.ext" / "quarry.yaml").read_text()
        )
        assert not (identities / "kpz.ext").exists()

        with patch(_NO_ETHOS, unpinned_root / "no-ethos"):
            again = enable_project(unpinned_root, client)
        assert again.ethos.vendored_updated == []


class TestT8EnableSkipsEthosWhenMissing:
    def test_skips_when_identities_dir_missing(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "nonexistent-identities"):
            result = enable_project(project, client)

        assert result.ethos.skipped is True


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

    def test_symlinked_ancestor_config_removal_does_not_strand_import(
        self, tmp_path: Path
    ) -> None:
        """A symlinked-ancestor config-remove must not abort disable and strand.

        The config-remove refusal is caught so disable continues to prune the
        @-import a prior deregister already acted on — and never unlinks the
        config.md outside the repo via the symlink.
        """
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "CLAUDE.md").write_text(f"# rules\n{REPO_IMPORT_LINE}\n")
        external = tmp_path / "external"
        (external / "quarry").mkdir(parents=True)
        planted = external / "quarry" / "config.md"
        planted.write_text("external config\n")
        (project / ".punt-labs").symlink_to(external)
        client = FakeRegistryClient([("myproject", project)])

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result = disable_project(project, client)

        # The import was pruned (not stranded), the external config survives,
        # and the refused config-remove is reported as not-removed.
        assert REPO_IMPORT_LINE not in (project / "CLAUDE.md").read_text()
        assert result.config_removed is False
        assert planted.read_text() == "external config\n"


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

    def test_refuses_symlinked_ancestor(self, tmp_path: Path) -> None:
        """A symlinked .punt-labs ancestor makes the config write refuse, not escape."""
        repo = tmp_path / "repo"
        repo.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (repo / ".punt-labs").symlink_to(external)

        with pytest.raises(ValueError, match="ancestor"):
            _write_project_config(repo)

        assert not (external / "quarry" / "config.md").exists()


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
        from quarry.doctor_sync import SyncDiagnostics

        project = tmp_path / "myproject"
        project.mkdir()
        monkeypatch.chdir(project)
        registry_path = tmp_path / "registry.db"
        self._register(registry_path, project)

        result = SyncDiagnostics.enable_status(registry_path, str(project))

        assert result.passed is False
        assert "config.md missing" in result.message
        assert result.required is False

    def test_config_present_returns_passed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from quarry.doctor_sync import SyncDiagnostics

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

        result = SyncDiagnostics.enable_status(registry_path, str(project))

        assert result.passed is True
        assert "config.md missing" not in result.message


class TestEnableRegistersImportAndMarker:
    def test_enable_writes_import_marker_and_guide(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result = enable_project(project, client)

        # The enabled marker and the repo @-import are the §2.11 biconditional:
        # both present after enable.
        assert result.import_registered is True
        assert result.enabled_marker_written is True
        assert result.guide_deposited is True
        assert EnabledMarker(project).is_present()
        claudemd = project / "CLAUDE.md"
        assert claudemd.read_text().rstrip("\n").endswith(REPO_IMPORT_LINE)
        guide = project / ".punt-labs" / "quarry" / "CLAUDE.md"
        assert "Local semantic search is available via quarry." in guide.read_text()


class TestEnableEnsuresCapturesGitignore:
    def test_enable_writes_captures_gitignore_entry(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result = enable_project(project, client)

        assert result.gitignore_ensured is True
        gitignore = project / ".gitignore"
        assert gitignore.exists()
        assert CAPTURES_GITIGNORE_ENTRY in gitignore.read_text()

    def test_enable_gitignore_ensure_is_idempotent(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result1 = enable_project(project, client)
            result2 = enable_project(project, client)

        assert result1.gitignore_ensured is True
        assert result2.gitignore_ensured is False
        content = (project / ".gitignore").read_text()
        assert content.count(CAPTURES_GITIGNORE_ENTRY) == 1

    def test_enable_preserves_existing_gitignore_content(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        (project / ".gitignore").write_text("node_modules/\n*.pyc\n")
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_project(project, client)

        content = (project / ".gitignore").read_text()
        assert "node_modules/" in content
        assert "*.pyc" in content
        assert CAPTURES_GITIGNORE_ENTRY in content

    def test_enable_ensures_gitignore_before_writing_capture_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The .gitignore exclusion lands BEFORE config.md's compaction flag.

        config.md's ``compaction: true`` is what makes hook-triggered capture
        writes live; it has no dependency on gitignore/marker state. If
        config.md landed first, a failure between the two writes would leave
        the repo "capturing enabled, unprotected". Enablement().enable() must
        run — and therefore the .gitignore exclusion must exist — before
        config.md is ever written, so a failure at the config-write step
        still leaves the repo protected.
        """
        import quarry.enable as enable_module

        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        def boom(directory: Path) -> str:
            raise OSError("config write failed")

        monkeypatch.setattr(enable_module, "_write_project_config", boom)

        with (
            patch(_NO_ETHOS, tmp_path / "no-ethos"),
            pytest.raises(OSError, match="config write failed"),
        ):
            enable_project(project, client)

        # Enablement().enable() already ran and committed its protection
        # before the config write raised.
        assert CAPTURES_GITIGNORE_ENTRY in (project / ".gitignore").read_text()
        assert EnabledMarker(project).is_present()
        config_path = project / ".punt-labs" / "quarry" / "config.md"
        assert not config_path.exists()


class TestEnableImportIdempotent:
    def test_running_enable_twice_does_not_duplicate(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result1 = enable_project(project, client)
            result2 = enable_project(project, client)

        assert result1.import_registered is True
        assert result2.import_registered is False
        content = (project / "CLAUDE.md").read_text()
        assert content.count(REPO_IMPORT_LINE) == 1


class TestEnableAppendsToExistingClaudemd:
    def test_existing_content_preserved(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        claudemd = project / "CLAUDE.md"
        claudemd.write_text("# My Project\n\nExisting content.\n")
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            result = enable_project(project, client)

        assert result.import_registered is True
        content = claudemd.read_text()
        assert content.startswith("# My Project\n\nExisting content.\n")
        assert content.rstrip("\n").endswith(REPO_IMPORT_LINE)


class TestDisableRemovesImportAndMarker:
    def test_disable_prunes_import_and_marker(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, tmp_path / "no-ethos"):
            enable_project(project, client)
            result = disable_project(project, client)

        assert result.import_pruned is True
        assert result.enabled_marker_removed is True
        assert not EnabledMarker(project).is_present()
        content = (project / "CLAUDE.md").read_text()
        assert REPO_IMPORT_LINE not in content
        # §2.9: the vendored guide is left dormant, not erased.
        assert (project / ".punt-labs" / "quarry" / "CLAUDE.md").exists()


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

        assert result.import_pruned is True
        content = claudemd.read_text()
        assert "# My Project" in content
        assert "Keep this." in content
        assert REPO_IMPORT_LINE not in content
