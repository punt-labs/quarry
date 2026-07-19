"""CLI integration tests for quarry enable/disable.

enable/disable go through the daemon registry via the injected client; these
tests patch ``TargetResolver.connect`` to a stateful ``FakeRegistryClient`` (from
conftest) so no real ``SyncRegistry`` or daemon is involved — hermetic, since CI
has no daemon.  The fake persists across an enable→disable pair in one context, so
a registration written by enable is visible to the subsequent disable.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.client import TargetResolver
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
        patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
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
    def test_disable_unregistered_exits_1(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        with _patch_for_cli(tmp_path):
            result = runner.invoke(app, ["disable", str(project)])

        assert result.exit_code == 1
        assert "no registration covers" in result.output


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


class TestT3bEnableCLIChildExits1:
    def test_child_of_parent_exits_1(self, tmp_path: Path) -> None:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()

        # Seed the daemon view so the parent covers the child: enabling the child
        # must refuse (the child uses the parent's collection automatically).
        seeded = FakeRegistryClient([("project", parent)])
        with _patch_for_cli(tmp_path, seeded):
            result = runner.invoke(app, ["enable", str(child)])

        assert result.exit_code == 1
        assert "already covered" in result.output
        assert seeded.registered == []
