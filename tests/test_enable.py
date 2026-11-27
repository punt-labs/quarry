"""Tests for the enable/disable module.

enable/disable drive the daemon's registry through a client port
(``RegistryClient``); these tests supply the in-memory ``FakeRegistryClient``
(from conftest) so no real ``SyncRegistry`` or daemon is involved.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from quarry._stdlib import load_hook_config
from quarry.claude_permissions import QUARRY_RULES
from quarry.disable_report import DisableReport
from quarry.enable import DisableResult, EnableResult, disable_project, enable_project
from quarry.enabled_marker import EnabledMarker
from quarry.gitignore import CAPTURES_GITIGNORE_ENTRY
from quarry.guidance import REPO_IMPORT_LINE
from quarry.marker_search import MarkerSearch
from quarry.project_config import ProjectConfig
from tests.conftest import FakeRegistryClient

pytestmark = pytest.mark.usefixtures("confined_config_reads")

_NO_ETHOS = "quarry.home_paths.QuarryHome.ethos_identities"


class TestT1EnableNewDirectory:
    def test_registers_new_directory(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            result = enable_project(project, client)

        assert result.collection == "foo"
        assert result.created_registration is False
        assert client.registered == []


class TestT3EnableCoveredChild:
    """A directory under a registered parent reuses the parent's collection."""

    @staticmethod
    def _covered(tmp_path: Path) -> tuple[Path, Path, FakeRegistryClient]:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()
        return parent, child, FakeRegistryClient([("project", parent)])

    def test_reuses_parent_collection_without_registering(self, tmp_path: Path) -> None:
        parent, child, client = self._covered(tmp_path)

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            result = enable_project(child, client)

        assert result.collection == "project"
        assert result.captures_collection == "project-captures"
        assert result.created_registration is False
        assert result.covered_by == str(parent)
        assert client.registered == []

    def test_writes_per_repo_files(self, tmp_path: Path) -> None:
        _, child, client = self._covered(tmp_path)

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            result = enable_project(child, client)

        config = child / ".punt-labs" / "quarry" / "config.md"
        assert config.is_file()
        assert result.config_path == str(config)
        assert EnabledMarker(child).is_present()

    def test_second_run_changes_nothing(self, tmp_path: Path) -> None:
        _, child, client = self._covered(tmp_path)
        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enable_project(child, client)
            before = _tree_snapshot(child)
            again = enable_project(child, client)

        assert _tree_snapshot(child) == before
        assert again.enabled_marker_written is False
        assert again.import_registered is False
        assert client.registered == []

    def test_doctor_is_clean_after_enable(self, tmp_path: Path) -> None:
        from quarry.doctor_sync import SyncDiagnostics
        from quarry.sync_registry import SyncRegistry

        parent, child, client = self._covered(tmp_path)
        registry_path = tmp_path / "registry.db"
        conn = SyncRegistry(registry_path)
        conn.register_directory(parent, "project")
        conn.close()

        before = SyncDiagnostics.enable_status(registry_path, str(child))
        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enable_project(child, client)
        after = SyncDiagnostics.enable_status(registry_path, str(child))

        assert before.passed is False
        assert "run 'quarry enable'" in before.message
        assert after.passed is True
        assert "quarry enable" not in after.message
        assert str(parent) in after.message


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class TestT4EnableCollectionOverride:
    def test_collection_override(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            result = enable_project(dir_b, client)

        assert result.collection == "backend-other"  # disambiguated, no merge
        assert result.collection != "backend"
        assert client.registered[-1].collection == "backend-other"


class TestT5EnableCreatesConfig:
    def test_creates_config_file(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=unpinned_root / "no-ethos"):
            result = enable_project(unpinned_root, client)

        assert result.ethos.vendored_updated == ["claude", "rmh"]
        assert (
            "## Memory (quarry guide v2)"
            in (identities / "rmh.ext" / "quarry.yaml").read_text()
        )
        assert not (identities / "kpz.ext").exists()

        with patch(_NO_ETHOS, return_value=unpinned_root / "no-ethos"):
            again = enable_project(unpinned_root, client)
        assert again.ethos.vendored_updated == []


class TestT8EnableSkipsEthosWhenMissing:
    def test_skips_when_identities_dir_missing(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, return_value=tmp_path / "nonexistent-identities"):
            result = enable_project(project, client)

        assert result.ethos.skipped is True


class TestT9EnableCapturesCollectionName:
    def test_captures_collection_name(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            result = enable_project(project, client)

        assert result.captures_collection == f"{result.collection}-captures"


class TestT10DisableRemovesRegistration:
    def test_removes_registration(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enable_project(project, client)
            config_path = project / ".punt-labs" / "quarry" / "config.md"
            assert config_path.exists()

            result = disable_project(project, client)

        assert result.config_removed is True
        assert not config_path.exists()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
    def test_unremovable_config_fails_the_disable_before_any_other_step(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enable_project(project, client)
            quarry_dir = project / ".punt-labs" / "quarry"
            quarry_dir.chmod(0o555)
            try:
                with pytest.raises(PermissionError, match=r"config\.md"):
                    disable_project(project, client)
            finally:
                quarry_dir.chmod(0o755)

        assert (quarry_dir / "config.md").exists()
        assert EnabledMarker(project).is_present()
        assert client.deregistered == []

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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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
        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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
        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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


class TestT15DisableCoveredChild:
    """Disabling a covered child undoes its own enable and spares the parent."""

    def test_enable_then_disable_leaves_child_clean(self, tmp_path: Path) -> None:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()
        (child / "CLAUDE.md").write_text("# rules\n")
        settings = child / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"permissions": {"allow": ["Bash(make:*)"]}}))
        client = FakeRegistryClient([("project", parent)])

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enable_project(child, client)
            result = disable_project(child, client)

        assert not (child / ".punt-labs" / "quarry" / "config.md").exists()
        assert not EnabledMarker(child).is_present()
        assert REPO_IMPORT_LINE not in (child / "CLAUDE.md").read_text()
        assert json.loads(settings.read_text()) == {
            "permissions": {"allow": ["Bash(make:*)"], "ask": []}
        }
        assert result.config_removed is True
        assert result.enabled_marker_removed is True
        assert result.covered_by == str(parent)
        assert result.collection == ""

    def test_parent_registration_and_data_untouched(self, tmp_path: Path) -> None:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()
        client = FakeRegistryClient([("project", parent)])

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enable_project(child, client)
            disable_project(child, client)

        assert client.deregistered == []
        assert client.deleted == []
        assert client.collections == ["project"]


def _own_checkout(directory: Path) -> Path:
    """Make *directory* the root of its own git checkout without running git."""
    git = directory / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    return directory


def _files(root: Path) -> set[str]:
    """Return every path under *root* outside ``.git``, hidden files included."""
    return {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if ".git" not in p.relative_to(root).parts
    }


class TestDisableNeverEnabledLeavesNoTrace:
    """A disable with nothing to remove must not create the CLAUDE.md lock file."""

    def test_covered_checkout_tree_is_unchanged(self, tmp_path: Path) -> None:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "app"
        child.mkdir()
        _own_checkout(child)
        before = _files(child)

        disable_project(child, FakeRegistryClient([("project", parent)]))

        assert _files(child) == before

    def test_uncovered_directory_tree_is_unchanged(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# rules\n")
        before = _files(project)

        disable_project(project, FakeRegistryClient())

        assert _files(project) == before
        assert (project / "CLAUDE.md").read_text() == "# rules\n"

    def test_a_lone_import_without_the_marker_is_still_pruned(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        claude = project / "CLAUDE.md"
        claude.write_text(f"# rules\n{REPO_IMPORT_LINE}\n")

        result = disable_project(project, FakeRegistryClient())

        assert result.import_pruned is True
        assert claude.read_text() == "# rules\n"


class TestDisableCoveredChildKeepsCaptureOptOuts:
    """Disable must not turn an opt-out into capture the user declined."""

    @staticmethod
    def _enabled_child(tmp_path: Path) -> tuple[Path, FakeRegistryClient]:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "app"
        child.mkdir()
        _own_checkout(child)
        client = FakeRegistryClient([("project", parent)])
        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enable_project(child, client)
        return child, client

    def test_config_with_an_opt_out_survives_and_is_reported(
        self, tmp_path: Path
    ) -> None:
        child, client = self._enabled_child(tmp_path)
        config = child / ".punt-labs" / "quarry" / "config.md"
        config.write_text(
            config.read_text().replace("session_end: true", "session_end: false")
        )

        result = disable_project(child, client)

        assert load_hook_config(str(child)).session_end is False
        assert result.config_removed is False
        assert result.config_kept is True
        text = "\n".join(DisableReport(result, keep_data=False).lines())
        assert "Config file kept" in text

    def test_untouched_template_config_is_removed(self, tmp_path: Path) -> None:
        child, client = self._enabled_child(tmp_path)

        result = disable_project(child, client)

        assert not (child / ".punt-labs" / "quarry" / "config.md").exists()
        assert result.config_removed is True
        assert result.config_kept is False

    def test_uncovered_directory_always_removes_its_config(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()
        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enable_project(project, client)
        config = project / ".punt-labs" / "quarry" / "config.md"
        assert config.read_text() == ProjectConfig().text
        config.write_text(
            config.read_text().replace("web_fetch: true", "web_fetch: false")
        )

        result = disable_project(project, client)

        assert not config.exists()
        assert result.config_removed is True


class TestDisableCoveredChildReport:
    """The disable report for a covered child reflects the parent's marker."""

    @staticmethod
    def _parent_child(tmp_path: Path, *, parent_marker: bool) -> tuple[Path, Path]:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()
        if parent_marker:
            EnabledMarker(parent).write()
        return parent, child

    def test_never_enabled_child_removes_nothing_and_names_parent(
        self, tmp_path: Path
    ) -> None:
        parent, child = self._parent_child(tmp_path, parent_marker=True)
        client = FakeRegistryClient([("project", parent)])

        result = disable_project(child, client)
        text = "\n".join(DisableReport(result, keep_data=False).lines())

        assert "Disabled" not in text
        assert f"`quarry disable {parent}` ends it but deregisters" in text
        assert f"purges everything indexed for {parent}" in text
        assert "--keep-data" in text
        assert client.deregistered == []
        assert client.deleted == []

    def test_enabled_then_disabled_child_lists_removals_and_still_active(
        self, tmp_path: Path
    ) -> None:
        parent, child = self._parent_child(tmp_path, parent_marker=True)
        client = FakeRegistryClient([("project", parent)])

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enable_project(child, client)
            result = disable_project(child, client)
        lines = DisableReport(result, keep_data=False).lines()
        text = "\n".join(lines)

        assert "Config file removed" in text
        assert "Removed enabled marker" in text
        assert "Disabled" not in text
        assert lines[-1].endswith("(add --keep-data to keep the data)")

    def test_parent_registered_without_marker_does_not_activate_session_start(
        self, tmp_path: Path
    ) -> None:
        parent, child = self._parent_child(tmp_path, parent_marker=False)
        client = FakeRegistryClient([("project", parent)])

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enable_project(child, client)
            result = disable_project(child, client)
        text = "\n".join(DisableReport(result, keep_data=False).lines())

        assert result.capturing_marker == ""
        assert (
            f"registered under {parent} but session start does not activate quarry"
            in text
        )
        assert f"quarry disable {parent}" not in text
        assert "still active" not in text

    def test_second_disable_converges_to_nothing_found(self, tmp_path: Path) -> None:
        parent, child = self._parent_child(tmp_path, parent_marker=True)
        client = FakeRegistryClient([("project", parent)])

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enable_project(child, client)
            disable_project(child, client)
            second = disable_project(child, client)
        text = "\n".join(DisableReport(second, keep_data=False).lines())

        assert "Nothing of its own found" in text
        assert "Config file removed" not in text
        assert f"`quarry disable {parent}` ends it but deregisters" in text
        assert f"purges everything indexed for {parent}" in text
        assert "--keep-data" in text


class TestT17EnableWithOverrideOnChildRaises:
    def test_conflicting_override_is_refused(self, tmp_path: Path) -> None:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()
        client = FakeRegistryClient([("project", parent)])

        with (
            patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"),
            pytest.raises(ValueError, match="already covered by the registration"),
        ):
            enable_project(child, client, collection_override="custom")

        assert not (child / ".punt-labs").exists()


class TestT18EnableResolvesRelativePath:
    def test_enable_with_relative_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        monkeypatch.chdir(project)
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

    def test_covered_directory_names_the_covering_registration(
        self, tmp_path: Path
    ) -> None:
        from quarry.doctor_sync import SyncDiagnostics

        parent = tmp_path / "myproject"
        child = parent / "src"
        (child / ".punt-labs" / "quarry").mkdir(parents=True)
        (child / ".punt-labs" / "quarry" / "config.md").write_text("---\n---\n")
        registry_path = tmp_path / "registry.db"
        self._register(registry_path, parent)

        result = SyncDiagnostics.enable_status(registry_path, str(child))

        assert result.passed is True
        assert f"registration: {parent}" in result.message
        assert "collection: myproject" in result.message


class TestEnableRegistersImportAndMarker:
    def test_enable_writes_import_marker_and_guide(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            result = enable_project(project, client)

        assert result.gitignore_ensured is True
        gitignore = project / ".gitignore"
        assert gitignore.exists()
        assert CAPTURES_GITIGNORE_ENTRY in gitignore.read_text()

    def test_enable_gitignore_ensure_is_idempotent(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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
        from quarry.project_config import ProjectConfig

        project = tmp_path / "myproject"
        project.mkdir()
        client = FakeRegistryClient()

        def boom(self: ProjectConfig, directory: Path) -> str:
            raise OSError("config write failed")

        monkeypatch.setattr(ProjectConfig, "write", boom)

        with (
            patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"),
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
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

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enable_project(project, client)
            result = disable_project(project, client)

        assert result.import_pruned is True
        content = claudemd.read_text()
        assert "# My Project" in content
        assert "Keep this." in content
        assert REPO_IMPORT_LINE not in content


class TestEnableGrantsProjectPermissions:
    """``quarry enable`` is the consent that grants quarry's allow rules (#552)."""

    def test_enable_grants_then_disable_revokes(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        settings = project / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"permissions": {"allow": ["Bash(make:*)"]}}))
        client = FakeRegistryClient()

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enabled = enable_project(project, client)
            again = enable_project(project, client)
            disabled = disable_project(project, client)

        rules = (*QUARRY_RULES["allow"], *QUARRY_RULES["ask"])
        assert enabled.permissions.changed == rules
        assert again.permissions.changed == ()
        assert disabled.permissions.changed == rules
        assert json.loads(settings.read_text()) == {
            "permissions": {"allow": ["Bash(make:*)"], "ask": []}
        }

    def test_unreadable_settings_does_not_fail_enable(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        (project / ".claude").mkdir()
        (project / ".claude" / "settings.json").write_text("{broken")

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            result = enable_project(project, FakeRegistryClient())

        assert result.enabled_marker_written is True
        assert "not valid JSON" in result.permissions.error


class TestDisableCoveredChildCapturedByIntermediateMarker:
    """A marker between the child and the registration root still captures it."""

    def test_intermediate_marker_is_named_and_command_targets_it(
        self, tmp_path: Path
    ) -> None:
        parent = tmp_path / "p"
        team = parent / "team"
        svc = team / "svc"
        svc.mkdir(parents=True)
        client = FakeRegistryClient([("p", parent)])

        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enable_project(team, client)
            enable_project(svc, client)
            result = disable_project(svc, client)
        text = "\n".join(DisableReport(result, keep_data=False).lines())

        assert "does not activate quarry" not in text
        assert f"through the marker at {team}" in text
        assert f"`quarry disable {team}`" in text
        assert f"`quarry disable {parent}`" not in text
        assert not EnabledMarker(svc).is_present()
        assert EnabledMarker(team).is_present()


class TestDisableCoveredChildCapturedByTwoMarkers:
    """Disabling the nearest of two markers leaves the next one found."""

    def test_intermediate_and_registration_markers_are_both_named(
        self, tmp_path: Path
    ) -> None:
        parent = tmp_path / "p"
        mid = parent / "a"
        leaf = mid / "b"
        leaf.mkdir(parents=True)
        EnabledMarker(parent).write()
        EnabledMarker(mid).write()
        client = FakeRegistryClient([("p", parent)])

        result = disable_project(leaf, client)
        text = "\n".join(DisableReport(result, keep_data=False).lines())

        assert result.capturing_marker == str(mid)
        assert result.also_capturing == (str(parent),)
        assert f"still active through the markers at {mid}, {parent}" in text
        assert "until all of them are disabled" in text

        disable_project(mid, client)
        assert MarkerSearch(leaf, str(parent)).found_at() == parent


class TestDisableCoveredChildConfigReport:
    """What the disable report says about a covered child's config.md."""

    @staticmethod
    def _disable_with_config(tmp_path: Path, write: str | bytes | None) -> list[str]:
        parent = tmp_path / "project"
        child = parent / "src"
        child.mkdir(parents=True)
        EnabledMarker(parent).write()
        client = FakeRegistryClient([("project", parent)])
        with patch(_NO_ETHOS, return_value=tmp_path / "no-ethos"):
            enable_project(child, client)
            config = child / ".punt-labs" / "quarry" / "config.md"
            if isinstance(write, bytes):
                config.write_bytes(write)
            elif isinstance(write, str):
                config.unlink()
                config.symlink_to(write)
            result = disable_project(child, client)
        return DisableReport(result, keep_data=False).lines()

    def test_untouched_config_written_by_an_older_template_is_removed(
        self, tmp_path: Path
    ) -> None:
        old = (
            ProjectConfig()
            .text.replace("  session_end: true\n", "")
            .replace("  web_search: true\n", "")
            .replace("  read: false\n", "")
            .replace("  subagent_stop: true\n", "")
        )
        lines = self._disable_with_config(tmp_path, old.encode())

        assert lines[0] == f"Removed quarry's own settings from {tmp_path}/project/src"
        assert "  Config file removed" in lines
        assert not any("kept" in line for line in lines)

    def test_unreadable_config_is_kept_and_said_to_be_unapplied(
        self, tmp_path: Path
    ) -> None:
        lines = self._disable_with_config(tmp_path, b"\xff\xfe\x00bad")
        text = "\n".join(lines)

        assert "could not be read" in text
        assert "not applied" in text
        assert "has settings of yours" not in text
        assert "still governs" not in text

    def test_symlinked_config_is_reported_as_left_in_place(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "shared.md"
        target.write_text(ProjectConfig().text)
        lines = self._disable_with_config(tmp_path, str(target))
        text = "\n".join(lines)

        assert (
            "config.md is behind a symlink (the file or a parent directory) "
            "and was left in place"
        ) in text
        assert "Nothing of its own found" not in text
        assert target.exists()

    def test_symlinked_parent_directory_is_reported_as_behind_a_symlink(
        self, tmp_path: Path
    ) -> None:
        parent = tmp_path / "project"
        child = parent / "src"
        child.mkdir(parents=True)
        EnabledMarker(parent).write()
        shared = tmp_path / "shared" / "quarry"
        shared.mkdir(parents=True)
        (shared / "config.md").write_text(ProjectConfig().text)
        (child / ".punt-labs").mkdir()
        (child / ".punt-labs" / "quarry").symlink_to(shared)
        client = FakeRegistryClient([("project", parent)])

        result = disable_project(child, client)
        text = "\n".join(DisableReport(result, keep_data=False).lines())

        assert "config.md is behind a symlink" in text
        assert "config.md is a symlink" not in text
        assert (shared / "config.md").exists()
