"""Behaviour of :mod:`quarry.skills_install`."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from quarry.skills_install import Harness, SkillOutcome, SkillsInstaller
from quarry.skills_source import SkillDepositError, SkillSource

if TYPE_CHECKING:
    from collections.abc import Iterable

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _write_skill(root: Path, name: str, body: str = "hello") -> None:
    """Write a minimal ``<root>/<name>/SKILL.md`` (+ one reference file)."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n\n{body}\n", encoding="utf-8"
    )
    references = skill_dir / "references"
    references.mkdir()
    (references / "detail.md").write_text("detail\n", encoding="utf-8")


def _write_pyproject(root: Path, package_name: str) -> None:
    """Write a minimal ``pyproject.toml`` naming *package_name*."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{package_name}"\n', encoding="utf-8"
    )


def _source_dir(
    tmp_path: Path,
    names: Iterable[str] = ("demo-a", "demo-b"),
    *,
    own: bool = True,
) -> Path:
    """Build ``<tmp_path>/repo/plugin/skills/`` and return it via ``locate_source``.

    ``own=True`` (the default) stamps the repo as quarry's OWN checkout --
    ``locate_source`` refuses any tree it cannot prove is quarry's, so every
    fixture exercising a real install/status/remove path needs one unless a
    test is specifically exercising that refusal (``own=False``).
    """
    root = tmp_path / "repo" / "plugin" / "skills"
    root.mkdir(parents=True)
    for name in names:
        _write_skill(root, name)
    if not own:
        # No pyproject.toml at all -- ``locate_source`` would refuse this
        # tree, so callers exercising that refusal need the raw path, not
        # a round trip through the guard they are testing.
        return root
    _write_pyproject(tmp_path / "repo", "punt-quarry")
    return SkillsInstaller.locate_source(tmp_path / "repo")


class TestLocateSource:
    def test_finds_the_real_repo_tree(self) -> None:
        source_dir = SkillsInstaller.locate_source(_REPO_ROOT)
        names = {p.name for p in source_dir.iterdir() if (p / "SKILL.md").is_file()}
        assert names >= {"quarry-recall", "quarry-capture"}

    def test_walks_upward_from_a_nested_start(self, tmp_path: Path) -> None:
        expected = _source_dir(tmp_path)
        nested = tmp_path / "repo" / "src" / "quarry"
        nested.mkdir(parents=True)
        assert SkillsInstaller.locate_source(nested) == expected

    def test_raises_when_no_ancestor_has_a_skills_tree(self) -> None:
        # A path guaranteed not to exist and not to sit under this repo
        # checkout -- unlike tmp_path here, whose ancestors include the repo
        # root, which genuinely has a plugin/skills/ tree.
        outside = Path("/quarry-test-no-such-tree-xyzzy/nested")
        with pytest.raises(FileNotFoundError, match="plugin/skills"):
            SkillsInstaller.locate_source(outside)


class TestLocateSourceIdentityGuard:
    """§ safety: install never deposits from a source it cannot prove is quarry's own.

    Mirrors the disable-side ownership guard (``TestDisableRetractsOwnSkills``
    in ``test_enablement.py``): shape alone (a ``plugin/skills/`` tree) proves
    nothing, since every marketplace-layout Claude Code plugin ships an
    identically-shaped tree.
    """

    def test_refuses_a_tree_with_no_pyproject_toml_at_all(self, tmp_path: Path) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",), own=False)
        with pytest.raises(
            FileNotFoundError, match="not part of quarry's own checkout"
        ):
            SkillsInstaller.locate_source(source_dir.parent.parent)

    def test_refuses_a_tree_belonging_to_a_different_package(
        self, tmp_path: Path
    ) -> None:
        """The blocking safety hole: an unrelated marketplace-layout plugin
        (e.g. lux) shipping the identically-shaped tree must never be
        accepted as an install source."""
        source_dir = _source_dir(tmp_path, names=("demo-a",), own=False)
        _write_pyproject(source_dir.parent.parent, "punt-lux")

        with pytest.raises(
            FileNotFoundError, match="not part of quarry's own checkout"
        ):
            SkillsInstaller.locate_source(source_dir.parent.parent)

    def test_accepts_a_tree_naming_the_real_package(self, tmp_path: Path) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",), own=True)
        assert SkillsInstaller.locate_source(source_dir.parent.parent) == source_dir


class TestHarnessPaths:
    @pytest.fixture(autouse=True)
    def _no_codex_home_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep ``CODEX_HOME`` unset so these default-path tests are hermetic
        regardless of what the host running the suite has exported."""
        monkeypatch.delenv("CODEX_HOME", raising=False)

    def test_claude_has_no_deposit_root(self, tmp_path: Path) -> None:
        assert Harness.CLAUDE.deposit_root(tmp_path) is None

    def test_pi_deposit_root(self, tmp_path: Path) -> None:
        expected = tmp_path / ".pi" / "agent" / "skills"
        assert Harness.PI.deposit_root(tmp_path) == expected

    def test_opencode_deposit_root(self, tmp_path: Path) -> None:
        assert (
            Harness.OPENCODE.deposit_root(tmp_path)
            == tmp_path / ".config" / "opencode" / "skills"
        )

    def test_codex_deposit_root_is_a_sibling_of_dot_system_not_inside_it(
        self, tmp_path: Path
    ) -> None:
        """Codex reserves ``.system/`` for its own preinstalled skills."""
        root = Harness.CODEX.deposit_root(tmp_path)
        assert root == tmp_path / ".codex" / "skills"
        assert root is not None
        assert ".system" not in root.parts

    def test_detected_requires_the_harness_marker_directory(
        self, tmp_path: Path
    ) -> None:
        assert Harness.CODEX.detected(tmp_path) is False
        (tmp_path / ".codex").mkdir()
        assert Harness.CODEX.detected(tmp_path) is True

    def test_codex_deposit_root_honors_codex_home_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-default-configured codex must be deposited to where IT
        loads from, not the hardcoded ``~/.codex`` default."""
        codex_home = tmp_path / "custom-codex"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        assert Harness.CODEX.deposit_root(tmp_path / "home") == codex_home / "skills"

    def test_codex_detected_honors_codex_home_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        codex_home = tmp_path / "custom-codex"
        codex_home.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        assert Harness.CODEX.detected(tmp_path / "home") is True

    def test_codex_not_detected_when_codex_home_env_var_points_elsewhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nonexistent-codex-home"))
        (tmp_path / "home" / ".codex").mkdir(parents=True)  # the default, unused

        assert Harness.CODEX.detected(tmp_path / "home") is False

    def test_install_deposits_under_codex_home_when_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        codex_home = tmp_path / "custom-codex"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        installer = SkillsInstaller(source_dir, tmp_path / "home")

        installer.install(Harness.CODEX)

        assert (codex_home / "skills" / "demo-a" / "SKILL.md").is_file()
        assert not (tmp_path / "home" / ".codex").exists()


class TestInstall:
    def test_deposits_every_skill_with_a_manifest_and_ignores_non_skill_dirs(
        self, tmp_path: Path
    ) -> None:
        source_dir = _source_dir(tmp_path)
        (source_dir / "not-a-skill").mkdir()
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)

        outcomes = installer.install(Harness.PI)

        assert {(o.skill, o.action) for o in outcomes} == {
            ("demo-a", "deposited"),
            ("demo-b", "deposited"),
        }
        target = home / ".pi" / "agent" / "skills" / "demo-a"
        assert (target / "SKILL.md").read_text(encoding="utf-8") == (
            source_dir / "demo-a" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert (target / "references" / "detail.md").is_file()
        assert (target / ".quarry-skill.json").is_file()

    def test_claude_is_unsupported_and_writes_nothing(self, tmp_path: Path) -> None:
        source_dir = _source_dir(tmp_path)
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)

        outcomes = installer.install(Harness.CLAUDE)

        assert all(o.action == "unsupported" and o.path is None for o in outcomes)
        assert not (home / ".claude").exists()

    def test_rerun_with_unchanged_content_is_a_noop(self, tmp_path: Path) -> None:
        source_dir = _source_dir(tmp_path)
        installer = SkillsInstaller(source_dir, tmp_path / "home")
        installer.install(Harness.CODEX)

        outcomes = installer.install(Harness.CODEX)

        assert all(o.action == "current" for o in outcomes)

    def test_changed_source_content_upgrades_on_reinstall(self, tmp_path: Path) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        installer = SkillsInstaller(source_dir, tmp_path / "home")
        installer.install(Harness.CODEX)

        (source_dir / "demo-a" / "SKILL.md").write_text(
            "---\nname: demo-a\ndescription: test\n---\n\nchanged\n", encoding="utf-8"
        )
        outcomes = installer.install(Harness.CODEX)

        assert outcomes == (
            SkillOutcome(
                Harness.CODEX,
                "demo-a",
                "upgraded",
                tmp_path / "home" / ".codex" / "skills" / "demo-a",
            ),
        )
        deposited = (
            tmp_path / "home" / ".codex" / "skills" / "demo-a" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert "changed" in deposited

    def test_install_detected_only_targets_detected_harnesses(
        self, tmp_path: Path
    ) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        (home / ".codex").mkdir(parents=True)
        installer = SkillsInstaller(source_dir, home)

        outcomes = installer.install_detected()

        assert {o.harness for o in outcomes} == {Harness.CODEX}
        assert (home / ".pi").exists() is False


class TestStatus:
    def test_absent_before_any_install(self, tmp_path: Path) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        installer = SkillsInstaller(source_dir, tmp_path / "home")

        statuses = installer.status()

        assert all(o.action in {"absent", "unsupported"} for o in statuses)

    def test_current_after_install_stale_after_source_changes(
        self, tmp_path: Path
    ) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        installer = SkillsInstaller(source_dir, tmp_path / "home")
        installer.install(Harness.OPENCODE)

        current = [
            o
            for o in installer.status()
            if o.harness is Harness.OPENCODE and o.skill == "demo-a"
        ]
        assert current == [
            SkillOutcome(
                Harness.OPENCODE,
                "demo-a",
                "current",
                tmp_path / "home" / ".config" / "opencode" / "skills" / "demo-a",
            )
        ]

        skill_md = source_dir / "demo-a" / "SKILL.md"
        skill_md.write_text("changed\n", encoding="utf-8")
        stale = [
            o
            for o in installer.status()
            if o.harness is Harness.OPENCODE and o.skill == "demo-a"
        ]
        assert stale[0].action == "stale"


class TestRemove:
    def test_remove_deletes_a_deposited_skill(self, tmp_path: Path) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        installer.install(Harness.PI)

        outcomes = installer.remove(Harness.PI)

        target = home / ".pi" / "agent" / "skills" / "demo-a"
        assert outcomes == (SkillOutcome(Harness.PI, "demo-a", "removed", target),)
        assert not (home / ".pi" / "agent" / "skills" / "demo-a").exists()

    def test_remove_is_idempotent(self, tmp_path: Path) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        installer = SkillsInstaller(source_dir, tmp_path / "home")

        outcomes = installer.remove(Harness.PI)

        assert outcomes[0].action == "absent"

    def test_remove_all_covers_every_harness(self, tmp_path: Path) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        for harness in (Harness.PI, Harness.OPENCODE, Harness.CODEX):
            installer.install(harness)

        outcomes = installer.remove_all()

        deposited_harnesses = {
            o.harness for o in outcomes if o.action in {"removed", "unsupported"}
        }
        assert deposited_harnesses == set(Harness)
        for harness in (Harness.PI, Harness.OPENCODE, Harness.CODEX):
            root = harness.deposit_root(home)
            assert root is not None
            assert not (root / "demo-a").exists()


class TestDepositAtomicity:
    def test_a_failed_swap_restores_the_previous_deposit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bug class 1: an interrupted rename must never leave the target gone."""
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        installer.install(Harness.PI)
        target = home / ".pi" / "agent" / "skills" / "demo-a"
        original_text = (target / "SKILL.md").read_text(encoding="utf-8")

        (source_dir / "demo-a" / "SKILL.md").write_text("v2\n", encoding="utf-8")

        real_rename = Path.rename

        def _boom(self: Path, target_path: str | Path) -> Path:
            if self.name.startswith(".demo-a.tmp-"):
                msg = "disk full"
                raise OSError(msg)
            return real_rename(self, target_path)

        monkeypatch.setattr(Path, "rename", _boom)

        with pytest.raises(OSError, match="disk full"):
            installer.install(Harness.PI)

        assert target.is_dir()
        assert (target / "SKILL.md").read_text(encoding="utf-8") == original_text
        # No leftover backup/tmp siblings after the restore.
        leftovers = list(target.parent.glob(".demo-a.*"))
        assert leftovers == []

    def test_fresh_install_swap_failure_leaves_no_target_and_no_orphans(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No prior deposit to restore -- the original error propagates plain."""
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        target = home / ".pi" / "agent" / "skills" / "demo-a"

        real_rename = Path.rename

        def _boom(self: Path, target_path: str | Path) -> Path:
            if self.name.startswith(".demo-a.tmp-"):
                msg = "disk full"
                raise OSError(msg)
            return real_rename(self, target_path)

        monkeypatch.setattr(Path, "rename", _boom)

        with pytest.raises(OSError, match="disk full"):
            installer.install(Harness.PI)

        assert not target.exists()
        assert list(target.parent.glob(".demo-a.*")) == []

    def test_copytree_failure_leaves_no_orphan_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "permission denied"
            raise OSError(msg)

        monkeypatch.setattr("shutil.copytree", _boom)

        with pytest.raises(OSError, match="permission denied"):
            installer.install(Harness.PI)

        target = home / ".pi" / "agent" / "skills" / "demo-a"
        assert not target.exists()
        assert list(target.parent.glob(".demo-a.*")) == []

    def test_manifest_write_failure_leaves_no_orphan_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)

        real_write_text = Path.write_text

        def _boom(self: Path, *args: object, **kwargs: object) -> int:
            if self.name == ".quarry-skill.json":
                msg = "disk full"
                raise OSError(msg)
            return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "write_text", _boom)

        with pytest.raises(OSError, match="disk full"):
            installer.install(Harness.PI)

        target = home / ".pi" / "agent" / "skills" / "demo-a"
        assert not target.exists()
        assert list(target.parent.glob(".demo-a.*")) == []

    def test_double_failure_raises_a_chained_deposit_error_naming_the_backup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Swap fails AND restoring the backup also fails: the backup path is
        preserved and named in a chained :class:`SkillDepositError`, never a
        silent loss of the last-known-good copy."""
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        installer.install(Harness.PI)
        (source_dir / "demo-a" / "SKILL.md").write_text("v2\n", encoding="utf-8")

        real_rename = Path.rename

        def _boom(self: Path, target_path: str | Path) -> Path:
            if self.name.startswith(".demo-a.tmp-") or self.name.startswith(
                ".demo-a.bak-"
            ):
                msg = "disk full"
                raise OSError(msg)
            return real_rename(self, target_path)

        monkeypatch.setattr(Path, "rename", _boom)

        with pytest.raises(SkillDepositError) as excinfo:
            installer.install(Harness.PI)

        target = home / ".pi" / "agent" / "skills" / "demo-a"
        assert str(target.parent / ".demo-a.bak-") in str(excinfo.value.backup)
        assert excinfo.value.backup.is_dir()  # the last-known-good copy survives
        assert excinfo.value.__cause__ is not None

    def test_a_failed_backup_rename_cleans_up_the_freshly_copied_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure renaming the live target aside to its backup name happens
        BEFORE tmp is ever swapped in; the freshly-copied tmp must still be
        cleaned up immediately, not left for the next deposit's orphan sweep
        to eventually find."""
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        installer.install(Harness.PI)
        target = home / ".pi" / "agent" / "skills" / "demo-a"
        original_text = (target / "SKILL.md").read_text(encoding="utf-8")
        (source_dir / "demo-a" / "SKILL.md").write_text("v2\n", encoding="utf-8")

        real_rename = Path.rename

        def _boom(self: Path, target_path: str | Path) -> Path:
            if self == target:
                msg = "permission denied"
                raise OSError(msg)
            return real_rename(self, target_path)

        monkeypatch.setattr(Path, "rename", _boom)

        with pytest.raises(OSError, match="permission denied"):
            installer.install(Harness.PI)

        assert target.is_dir()  # the original deposit is untouched
        assert (target / "SKILL.md").read_text(encoding="utf-8") == original_text
        # no leaked tmp (or backup, which was never created) siblings
        assert list(target.parent.glob(".demo-a.*")) == []


class TestOrphanSweep:
    def test_a_stale_tmp_from_a_different_pid_is_swept_on_the_next_deposit(
        self, tmp_path: Path
    ) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        target_dir = home / ".pi" / "agent" / "skills"
        target_dir.mkdir(parents=True)
        orphan_tmp = target_dir / ".demo-a.tmp-999999"
        orphan_tmp.mkdir()
        (orphan_tmp / "leftover.txt").write_text("stale\n", encoding="utf-8")
        orphan_bak = target_dir / ".demo-a.bak-999999"
        orphan_bak.mkdir()

        installer.install(Harness.PI)

        assert not orphan_tmp.exists()
        assert not orphan_bak.exists()
        assert (target_dir / "demo-a" / "SKILL.md").is_file()

    def test_a_stale_backup_file_not_a_directory_is_also_swept(
        self, tmp_path: Path
    ) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        target_dir = home / ".pi" / "agent" / "skills"
        target_dir.mkdir(parents=True)
        stray_file = target_dir / ".demo-a.bak-123"
        stray_file.write_text("not a dir\n", encoding="utf-8")

        installer.install(Harness.PI)

        assert not stray_file.exists()

    def test_a_skill_named_with_a_glob_metacharacter_does_not_sweep_unrelated_siblings(
        self, tmp_path: Path
    ) -> None:
        """A skill literally named ``*`` must not turn the orphan-sweep glob
        into a wildcard matching -- and deleting -- an unrelated sibling's
        own orphaned temp directory."""
        source_dir = _source_dir(tmp_path, names=("*",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        target_dir = home / ".pi" / "agent" / "skills"
        target_dir.mkdir(parents=True)
        unrelated_orphan = target_dir / ".demo-a.tmp-999999"
        unrelated_orphan.mkdir()
        (unrelated_orphan / "keep.txt").write_text("keep\n", encoding="utf-8")

        installer.install(Harness.PI)

        assert unrelated_orphan.exists()  # untouched despite the "*" skill's sweep
        assert (target_dir / "*" / "SKILL.md").is_file()

    def test_sweep_orphans_escapes_a_glob_metacharacter_directly(
        self, tmp_path: Path
    ) -> None:
        """Unit-level regression for the escape itself, independent of a
        full install round trip."""
        parent = tmp_path / "skills"
        parent.mkdir()
        unrelated = parent / ".demo-a.tmp-12345"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("keep\n", encoding="utf-8")

        SkillSource._sweep_orphans(parent, "*")

        assert unrelated.exists()


class TestRemoveManifestOwnership:
    """§ safety: removal never touches a directory quarry didn't deposit."""

    def test_a_directory_lacking_a_quarry_manifest_is_refused_not_deleted(
        self, tmp_path: Path
    ) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        foreign = home / ".pi" / "agent" / "skills" / "demo-a"
        foreign.mkdir(parents=True)
        (foreign / "NOTES.md").write_text("not quarry's\n", encoding="utf-8")

        outcomes = installer.remove(Harness.PI)

        assert outcomes == (SkillOutcome(Harness.PI, "demo-a", "foreign", foreign),)
        assert foreign.is_dir()
        assert (foreign / "NOTES.md").is_file()

    def test_a_directory_with_a_corrupt_manifest_is_also_refused(
        self, tmp_path: Path
    ) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        foreign = home / ".pi" / "agent" / "skills" / "demo-a"
        foreign.mkdir(parents=True)
        (foreign / ".quarry-skill.json").write_text("not json{{{", encoding="utf-8")

        outcomes = installer.remove(Harness.PI)

        assert outcomes[0].action == "foreign"
        assert foreign.is_dir()

    def test_a_genuinely_deposited_directory_is_still_removed(
        self, tmp_path: Path
    ) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        installer.install(Harness.PI)
        target = home / ".pi" / "agent" / "skills" / "demo-a"
        assert target.is_dir()

        outcomes = installer.remove(Harness.PI)

        assert outcomes[0].action == "removed"
        assert not target.exists()


class TestCorruptManifest:
    """A hand-edited or corrupt ``.quarry-skill.json`` never crashes -- and,
    per the foreign-overwrite guard, is never silently upgraded either: a
    corrupt manifest is indistinguishable from "no manifest at all" (see
    :meth:`SkillsInstaller._is_foreign`), so both install and status treat
    it as foreign, not stale.
    """

    def test_install_refuses_to_overwrite_a_corrupt_manifest(
        self, tmp_path: Path
    ) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        target = home / ".pi" / "agent" / "skills" / "demo-a"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("stale\n", encoding="utf-8")
        (target / ".quarry-skill.json").write_text("{not valid json", encoding="utf-8")

        outcomes = installer.install(Harness.PI)

        assert outcomes == (SkillOutcome(Harness.PI, "demo-a", "foreign", target),)
        assert (target / "SKILL.md").read_text(encoding="utf-8") == "stale\n"

    def test_status_reports_foreign_over_a_corrupt_manifest(
        self, tmp_path: Path
    ) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        target = home / ".pi" / "agent" / "skills" / "demo-a"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("stale\n", encoding="utf-8")
        (target / ".quarry-skill.json").write_text(
            '{"content_hash": 12345}',
            encoding="utf-8",  # wrong type, not a string
        )

        statuses = [
            o
            for o in installer.status()
            if o.skill == "demo-a" and o.harness is Harness.PI
        ]

        assert statuses[0].action == "foreign"


class TestInstallForeignOverwriteGuard:
    """§ safety: install never clobbers a same-named directory it never deposited."""

    def test_install_refuses_a_directory_with_no_manifest_and_does_not_touch_it(
        self, tmp_path: Path
    ) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        foreign = home / ".pi" / "agent" / "skills" / "demo-a"
        foreign.mkdir(parents=True)
        (foreign / "NOTES.md").write_text("not quarry's\n", encoding="utf-8")

        outcomes = installer.install(Harness.PI)

        assert outcomes == (SkillOutcome(Harness.PI, "demo-a", "foreign", foreign),)
        assert (foreign / "NOTES.md").is_file()
        assert not (foreign / "SKILL.md").exists()

    def test_status_reports_foreign_for_a_directory_with_no_manifest(
        self, tmp_path: Path
    ) -> None:
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        home = tmp_path / "home"
        installer = SkillsInstaller(source_dir, home)
        foreign = home / ".pi" / "agent" / "skills" / "demo-a"
        foreign.mkdir(parents=True)
        (foreign / "NOTES.md").write_text("not quarry's\n", encoding="utf-8")

        statuses = [
            o
            for o in installer.status()
            if o.skill == "demo-a" and o.harness is Harness.PI
        ]

        assert statuses == [SkillOutcome(Harness.PI, "demo-a", "foreign", foreign)]


class TestSymlinkSafety:
    """§ safety: a symlink in the source tree is never dereferenced into the
    deposited copy -- a ``stolen.txt -> /secret`` link must land as a
    symlink, never as a regular file materializing the target's bytes."""

    def test_deposit_preserves_a_symlink_instead_of_dereferencing_it(
        self, tmp_path: Path
    ) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("do-not-leak\n", encoding="utf-8")
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        (source_dir / "demo-a" / "stolen.txt").symlink_to(secret)
        installer = SkillsInstaller(source_dir, tmp_path / "home")

        installer.install(Harness.PI)

        deposited = (
            tmp_path / "home" / ".pi" / "agent" / "skills" / "demo-a" / "stolen.txt"
        )
        assert deposited.is_symlink()
        assert deposited.readlink() == secret

    def test_content_hash_is_based_on_the_link_target_not_its_bytes(
        self, tmp_path: Path
    ) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("do-not-leak\n", encoding="utf-8")
        source_dir = _source_dir(tmp_path, names=("demo-a",))
        (source_dir / "demo-a" / "stolen.txt").symlink_to(secret)
        source = SkillSource(source_dir)
        content_hash = source.content_hash("demo-a")

        # Changing the SECRET's bytes (not the link itself) must not move
        # the hash -- the hash reflects what deposit() actually copies (the
        # link), not whatever the link happens to point at right now.
        secret.write_text("changed\n", encoding="utf-8")

        assert source.content_hash("demo-a") == content_hash
