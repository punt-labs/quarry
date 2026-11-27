"""CLI integration tests for quarry enable/disable.

enable/disable go through the daemon registry via the injected client; these
tests patch ``TargetResolver.connect`` to a stateful ``FakeRegistryClient`` (from
conftest) so no real ``SyncRegistry`` or daemon is involved — hermetic, since CI
has no daemon.  The fake persists across an enable→disable pair in one context, so
a registration written by enable is visible to the subsequent disable.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.client import TargetResolver
from quarry.enabled_marker import EnabledMarker
from tests.conftest import FakeRegistryClient

if TYPE_CHECKING:
    import pytest

runner = CliRunner()


@contextlib.contextmanager
def _patch_for_cli(
    tmp_path: Path, client: FakeRegistryClient | None = None
) -> Generator[FakeRegistryClient]:
    """Patch ethos identities off and the CLI client factory to a fake.

    Yields the fake so a test can seed coverage (pass one in) or assert on the
    calls it recorded.  ``TargetResolver.connect`` is the plumbing's actual
    factory — patching it keeps the command off ``resolve()`` and off a live
    daemon (a non-hermetic dependency that passes only where quarryd is up).
    """
    fake = FakeRegistryClient() if client is None else client
    with (
        patch(
            "quarry.home_paths.QuarryHome.ethos_identities",
            return_value=tmp_path / "no-ethos",
        ),
        patch.object(TargetResolver, "connect", return_value=fake),
    ):
        yield fake


class TestT21EnableCLIHappyPath:
    def test_enable_happy_path(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        with _patch_for_cli(tmp_path):
            result = runner.invoke(app, ["enable", str(project)])

        assert result.exit_code == 0, result.output
        assert "myproject" in result.output


class TestEnableCLIExpandsTilde:
    def test_tilde_path_resolves_against_home_not_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `quarry enable ~/proj` must resolve "~" against $HOME (enable_project's
        # expanduser().resolve()), NOT cwd — the CLI passes the raw path so the
        # tilde survives to the single normalization point. A bare `.resolve()`
        # at the CLI layer would target ./~/proj instead.
        home = tmp_path / "home"
        project = home / "proj"
        project.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        # Guarantee cwd is elsewhere, so a cwd-relative "~" would resolve wrong.
        monkeypatch.chdir(tmp_path)

        with _patch_for_cli(tmp_path):
            result = runner.invoke(app, ["--json", "enable", "~/proj"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["directory"] == str(project.resolve())
        assert "~" not in data["directory"]


class TestT22EnableCLICollectionOverride:
    def test_enable_with_collection_override(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        with _patch_for_cli(tmp_path):
            result = runner.invoke(
                app, ["enable", str(project), "--collection", "custom"]
            )

        assert result.exit_code == 0, result.output
        assert "custom" in result.output


class TestT23DisableCLIHappyPath:
    def test_disable_happy_path(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        # One stateful fake spans both calls: enable registers "myproject", so
        # the subsequent disable's list_registrations covers the dir.
        with _patch_for_cli(tmp_path) as fake:
            enable_result = runner.invoke(app, ["enable", str(project)])
            assert enable_result.exit_code == 0, enable_result.output

            disable_result = runner.invoke(app, ["disable", str(project)])

        assert disable_result.exit_code == 0, disable_result.output
        assert "Disabled" in disable_result.output
        # F&F: the daemon deregistered the collection and the captures purge was
        # dispatched — no await, no deleted-chunk count.
        assert [r.collection for r in fake.deregistered] == ["myproject"]
        assert fake.deleted == ["myproject-captures"]


class TestT24DisableCLIUnregistered:
    def test_disable_unregistered_is_idempotent_noop(self, tmp_path: Path) -> None:
        # Disabling a never-enabled directory is an idempotent no-op: exit 0 with
        # an "already disabled" note, not a "no registration covers" failure.
        project = tmp_path / "myproject"
        project.mkdir()

        with _patch_for_cli(tmp_path) as fake:
            result = runner.invoke(app, ["disable", str(project)])

        assert result.exit_code == 0, result.output
        assert "Already disabled" in result.output
        assert fake.deregistered == []


class TestJsonErrorPathKeepsStdoutEmpty:
    def test_enable_failure_emits_no_json_to_stdout(self, tmp_path: Path) -> None:
        # A failure in --json mode must not print a JSON error object to stdout;
        # it goes through _cli_errors (stderr only), so `quarry enable --json | jq`
        # never sees a spurious object.
        project = tmp_path / "p"
        project.mkdir()
        with patch(
            "quarry.enable.enable_project",
            side_effect=ValueError("no registration covers"),
        ):
            result = runner.invoke(app, ["--json", "enable", str(project)])
        assert result.exit_code == 1
        assert '"error"' not in result.output

    def test_disable_failure_emits_no_json_to_stdout(self, tmp_path: Path) -> None:
        project = tmp_path / "p"
        project.mkdir()
        with patch(
            "quarry.enable.disable_project",
            side_effect=ValueError("no registration covers"),
        ):
            result = runner.invoke(app, ["--json", "disable", str(project)])
        assert result.exit_code == 1
        assert '"error"' not in result.output


class TestT25EnableCLIJsonOutput:
    def test_enable_json_output(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        with _patch_for_cli(tmp_path):
            result = runner.invoke(app, ["--json", "enable", str(project)])

        assert result.exit_code == 0, result.output

        data = json.loads(result.stdout)
        assert "directory" in data
        assert "collection" in data
        assert "captures_collection" in data
        assert "created_registration" in data
        assert data["collection"] == "myproject"
        assert data["captures_collection"] == "myproject-captures"
        assert data["created_registration"] is True


class TestSkillsDepositedJsonSerializable:
    def test_enable_json_output_serializes_skills_deposited(
        self, tmp_path: Path
    ) -> None:
        """A SkillOutcome.path is a Path; --json must still emit valid JSON.

        EnableResult.skills_deposited nests SkillOutcome,
        whose path field is a pathlib.Path -- the bare dataclasses.asdict()
        default factory leaves it un-serializable and json.dump raises at the
        CLI's --json boundary (TypeError: Object of type PosixPath is not
        JSON serializable) unless the emit path stringifies it.
        """
        from quarry.enablement import Enablement
        from quarry.enablement_result import EnablementResult
        from quarry.skills_install import Harness, SkillOutcome

        project = tmp_path / "myproject"
        project.mkdir()
        real_enable = Enablement.enable

        def fake_enable(self: Enablement) -> EnablementResult:
            result = real_enable(self)
            deposited = SkillOutcome(
                Harness.PI, "quarry-recall", "deposited", tmp_path / "pi-deposit"
            )
            return dataclasses.replace(result, skills_deposited=(deposited,))

        with (
            _patch_for_cli(tmp_path),
            patch.object(Enablement, "enable", fake_enable),
        ):
            result = runner.invoke(app, ["--json", "enable", str(project)])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)  # raises if the payload wasn't valid JSON
        assert data["skills_deposited"] == [
            {
                "harness": "pi",
                "skill": "quarry-recall",
                "action": "deposited",
                "path": str(tmp_path / "pi-deposit"),
            }
        ]


class TestEnableCLICoveredByParent:
    """A new repo under a registered parent enables with the parent's collection."""

    @staticmethod
    def _seeded(tmp_path: Path) -> tuple[Path, Path, FakeRegistryClient]:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()
        EnabledMarker(parent).write()
        return parent, child, FakeRegistryClient([("project", parent)])

    def test_exits_0_naming_the_reused_registration(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        parent, child, seeded = self._seeded(tmp_path)

        with caplog.at_level(logging.ERROR), _patch_for_cli(tmp_path, seeded):
            result = runner.invoke(app, ["enable", str(child)])

        assert result.exit_code == 0, result.output
        assert f"Reused registration at {parent}" in result.output
        assert "Collection: project" in result.output
        assert "Traceback" not in result.output
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
        assert seeded.registered == []
        assert (child / ".punt-labs" / "quarry" / "config.md").is_file()

    def test_json_carries_covered_by(self, tmp_path: Path) -> None:
        parent, child, seeded = self._seeded(tmp_path)

        with _patch_for_cli(tmp_path, seeded):
            result = runner.invoke(app, ["--json", "enable", str(child)])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["covered_by"] == str(parent)
        assert data["collection"] == "project"
        assert data["created_registration"] is False

    def test_disable_child_names_parent_command_and_spares_registration(
        self, tmp_path: Path
    ) -> None:
        parent, child, seeded = self._seeded(tmp_path)

        with _patch_for_cli(tmp_path, seeded):
            result = runner.invoke(app, ["disable", str(child)])

        assert result.exit_code == 0, result.output
        assert "Disabled" not in result.output
        assert f"'quarry disable {parent}' stops it but deregisters" in result.output
        assert f"purges everything indexed for {parent}" in result.output
        assert "--keep-data" in result.output
        assert seeded.deregistered == []

    def test_disable_child_json_carries_covered_by(self, tmp_path: Path) -> None:
        parent, child, seeded = self._seeded(tmp_path)

        with _patch_for_cli(tmp_path, seeded):
            result = runner.invoke(app, ["--json", "disable", str(child)])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["covered_by"] == str(parent)
        assert seeded.deregistered == []
