"""Direct unit tests for :mod:`quarry.skills_sync`'s two collaborators.

``tests/test_enablement.py`` already exercises both through
:class:`~quarry.enablement.Enablement`'s public ``enable``/``disable``
surface (the real-world entry point); these tests pin the collaborators'
own behavior -- deposit's packaged source (PL-1049) and retraction's
own-checkout guard, plus the best-effort failure discipline -- at the unit
level, independent of ``Enablement``'s CLAUDE.md/marker/import machinery.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from quarry.home_paths import HomeUnresolvedError
from quarry.skills_install import Harness, SkillsInstaller
from quarry.skills_sync import SkillDeposit, SkillRetraction

if TYPE_CHECKING:
    import pytest


def _write_skill(root: Path, name: str) -> None:
    skill_dir = root / "plugin" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n\nbody\n", encoding="utf-8"
    )


def _write_pyproject(root: Path, package_name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{package_name}"\n', encoding="utf-8"
    )


class TestSkillDeposit:
    """Deposit reads quarry's OWN packaged tree, ungated by any repo."""

    def test_deposits_the_packaged_skills_into_every_detected_harness(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        (home / ".pi").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

        outcomes = SkillDeposit.run()

        assert {o.skill for o in outcomes} >= {"quarry-recall", "quarry-capture"}
        assert all(o.action == "deposited" for o in outcomes)
        assert (
            home / ".pi" / "agent" / "skills" / "quarry-recall" / "SKILL.md"
        ).is_file()

    def test_fires_in_an_ordinary_repo_with_no_plugin_skills_of_its_own(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The goal of PL-1049: a normal user's project, with no
        ``plugin/skills/`` and no ``punt-quarry`` pyproject of its own,
        still gets the skills deposited."""
        home = tmp_path / "home"
        (home / ".codex").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

        outcomes = SkillDeposit.run()

        deposited = [o.skill for o in outcomes if o.action == "deposited"]
        assert deposited

    def test_a_filesystem_failure_is_swallowed_and_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        home = tmp_path / "home"
        (home / ".pi").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

        def _boom(_self: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(SkillsInstaller, "install_detected", _boom)

        with caplog.at_level("WARNING", logger="quarry.skills_sync"):
            outcomes = SkillDeposit.run()  # must not raise

        assert outcomes == ()
        assert any("failed to deposit" in r.getMessage() for r in caplog.records)

    def test_an_unresolvable_home_is_swallowed_and_logged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An unset ``$HOME`` must not abort ``enable``."""

        def _boom() -> Path:
            raise HomeUnresolvedError("cannot determine the home directory")

        monkeypatch.setattr("quarry.skills_sync.QuarryHome.home", _boom)

        with caplog.at_level("WARNING", logger="quarry.skills_sync"):
            outcomes = SkillDeposit.run()  # must not raise

        assert outcomes == ()
        assert any("failed to deposit" in r.getMessage() for r in caplog.records)


class TestSkillRetraction:
    """Retraction keeps its narrower own-checkout gate -- unrelated to deposit."""

    def test_no_source_tree_retracts_nothing(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        SkillRetraction(repo).run()  # must not raise

    def test_own_checkout_retracts_a_prior_deposit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
        repo = tmp_path / "repo"
        _write_skill(repo, "demo")
        _write_pyproject(repo, "punt-quarry")
        SkillsInstaller(repo / "plugin" / "skills", home).install(Harness.CODEX)
        deposited = home / ".codex" / "skills" / "demo"
        assert deposited.is_dir()

        SkillRetraction(repo).run()

        assert not deposited.exists()

    def test_a_filesystem_failure_is_swallowed_and_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
        repo = tmp_path / "repo"
        _write_skill(repo, "demo")
        _write_pyproject(repo, "punt-quarry")
        SkillsInstaller(repo / "plugin" / "skills", home).install(Harness.CODEX)

        def _boom(_self: object) -> None:
            msg = "permission denied"
            raise OSError(msg)

        monkeypatch.setattr(SkillsInstaller, "remove_all", _boom)

        with caplog.at_level("WARNING", logger="quarry.skills_sync"):
            SkillRetraction(repo).run()  # must not raise

        assert any("failed to retract" in r.getMessage() for r in caplog.records)

    def test_an_unresolvable_home_is_swallowed_and_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An unset ``$HOME`` must not abort ``disable``."""
        repo = tmp_path / "repo"
        _write_skill(repo, "demo")
        _write_pyproject(repo, "punt-quarry")

        def _boom() -> Path:
            raise HomeUnresolvedError("cannot determine the home directory")

        monkeypatch.setattr("quarry.skills_sync.QuarryHome.home", _boom)

        with caplog.at_level("WARNING", logger="quarry.skills_sync"):
            SkillRetraction(repo).run()  # must not raise

        assert any("failed to retract" in r.getMessage() for r in caplog.records)
