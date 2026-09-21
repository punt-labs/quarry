"""Behaviour of :mod:`quarry.skills_install`."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from quarry.skills_install import Harness, SkillOutcome, SkillsInstaller

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


def _source_dir(tmp_path: Path, names: Iterable[str] = ("demo-a", "demo-b")) -> Path:
    root = tmp_path / "repo" / "plugin" / "skills"
    root.mkdir(parents=True)
    for name in names:
        _write_skill(root, name)
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


class TestHarnessPaths:
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
