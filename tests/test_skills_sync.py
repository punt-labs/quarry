"""Direct unit tests for :mod:`quarry.skills_sync`'s two collaborators.

``tests/test_enablement.py`` already exercises this through
:class:`~quarry.enablement.Enablement`'s public ``enable`` surface (the
real-world entry point); these tests pin :class:`SkillDeposit`'s own
behavior -- its packaged source and the best-effort failure
discipline -- at the unit level, independent of ``Enablement``'s
CLAUDE.md/marker/import machinery.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from quarry.home_paths import HomeUnresolvedError
from quarry.skills_install import Harness, SkillsInstaller
from quarry.skills_source import SkillSource
from quarry.skills_sync import SkillDeposit

if TYPE_CHECKING:
    import pytest


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
        """A normal user's project, with no
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


class TestInstallDetectedResilience:
    """A failure on one detected harness must not lose the others."""

    def test_one_harness_failure_keeps_the_others(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        (home / ".pi").mkdir(parents=True)
        (home / ".codex").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
        installer = SkillsInstaller(SkillSource.packaged(), home)
        real_install = SkillsInstaller.install

        def _install(self: SkillsInstaller, harness: Harness) -> tuple[object, ...]:
            if harness is Harness.PI:
                msg = "permission denied"
                raise OSError(msg)
            return real_install(self, harness)

        monkeypatch.setattr(SkillsInstaller, "install", _install)

        outcomes = installer.install_detected()

        # pi raised and was skipped; codex still got its deposit.
        harnesses = {o.harness for o in outcomes}
        assert Harness.CODEX in harnesses
        assert Harness.PI not in harnesses
        assert (home / ".codex" / "skills" / "quarry-recall" / "SKILL.md").is_file()
