"""Behavior of :meth:`quarry.ethos_handle.EthosConfig.agent_handle_at`.

The walk is quarry's; the new-file-then-legacy precedence at each ancestor is
ethos's (``.punt-labs/ethos.yaml`` wins over ``.punt-labs/ethos/config.yaml``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.ethos_handle import EthosConfig

if TYPE_CHECKING:
    import pytest


def _write_config(root: Path, body: str) -> None:
    """Deposit a legacy ethos config at ``root/.punt-labs/ethos/config.yaml``."""
    config_dir = root / ".punt-labs" / "ethos"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(body)


def _write_pin(root: Path, body: str) -> None:
    """Deposit the current ethos repo pin at ``root/.punt-labs/ethos.yaml``."""
    punt_labs = root / ".punt-labs"
    punt_labs.mkdir(parents=True, exist_ok=True)
    (punt_labs / "ethos.yaml").write_text(body)


class TestReadAgentHandle:
    def test_returns_agent_from_direct_config(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "agent: rmh\n")
        assert EthosConfig.agent_handle_at(str(tmp_path)) == "rmh"

    def test_walks_up_to_ancestor_config(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "agent: kpz\n")
        deep = tmp_path / "src" / "quarry" / "retrieval"
        deep.mkdir(parents=True)
        assert EthosConfig.agent_handle_at(str(deep)) == "kpz"

    def test_missing_file_returns_empty(self, unpinned_root: Path) -> None:
        # No pin anywhere on the walk — the walker must not raise, since the
        # empty string is the documented "no identity here" signal. tmp_path
        # would find this repo's own pin, hence the unpinned root.
        assert EthosConfig.agent_handle_at(str(unpinned_root)) == ""

    def test_missing_agent_field_returns_empty(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "other: value\n")
        assert EthosConfig.agent_handle_at(str(tmp_path)) == ""

    def test_non_string_agent_returns_empty(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "agent: 42\n")
        assert EthosConfig.agent_handle_at(str(tmp_path)) == ""

    def test_blank_agent_returns_empty(self, tmp_path: Path) -> None:
        _write_config(tmp_path, 'agent: ""\n')
        assert EthosConfig.agent_handle_at(str(tmp_path)) == ""

    def test_malformed_yaml_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_config(tmp_path, "agent: [unclosed\n")
        with caplog.at_level("WARNING", logger="quarry.ethos_handle"):
            assert EthosConfig.agent_handle_at(str(tmp_path)) == ""
        assert any("could not parse" in rec.message for rec in caplog.records)


class TestPinFilePrecedence:
    """Ethos reads ``.punt-labs/ethos.yaml`` first, then the legacy config."""

    def test_reads_the_current_pin_file(self, tmp_path: Path) -> None:
        _write_pin(tmp_path, "agent: claude\nteam: quarry\nresolution: repo-only\n")
        assert EthosConfig.agent_handle_at(str(tmp_path)) == "claude"

    def test_new_file_wins_over_legacy_at_the_same_ancestor(
        self, tmp_path: Path
    ) -> None:
        _write_pin(tmp_path, "agent: claude\n")
        _write_config(tmp_path, "agent: legacy\n")
        assert EthosConfig.agent_handle_at(str(tmp_path)) == "claude"

    def test_nearer_legacy_wins_over_farther_new_file(self, tmp_path: Path) -> None:
        """Precedence is per ancestor; the walk still stops at the nearest pin."""
        _write_pin(tmp_path, "agent: outer\n")
        inner = tmp_path / "inner"
        _write_config(inner, "agent: inner\n")
        assert EthosConfig.agent_handle_at(str(inner)) == "inner"

    def test_pin_file_found_from_a_subdirectory(self, tmp_path: Path) -> None:
        _write_pin(tmp_path, "agent: claude\n")
        deep = tmp_path / "src" / "quarry"
        deep.mkdir(parents=True)
        assert EthosConfig.agent_handle_at(str(deep)) == "claude"

    def test_malformed_pin_file_returns_empty_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_pin(tmp_path, "agent: [unclosed\n")
        with caplog.at_level("WARNING", logger="quarry.ethos_handle"):
            assert EthosConfig.agent_handle_at(str(tmp_path)) == ""
        assert any("ethos.yaml" in rec.getMessage() for rec in caplog.records)


class TestPinWalkBounds:
    """Attribution is repo-scoped: a checkout without a pin is unattributed.

    The alternative — adopting a parent directory's or the operator's global
    pin — would file a session's captures under an identity the session never
    declared.
    """

    def test_repo_without_a_pin_does_not_adopt_a_parent_pin(
        self, unpinned_root: Path
    ) -> None:
        _write_pin(unpinned_root, "agent: outer\n")
        child = unpinned_root / "child"
        (child / ".git").mkdir(parents=True)
        (child / "src").mkdir()
        assert EthosConfig.agent_handle_at(str(child / "src")) == ""
        assert EthosConfig.subagent_handle_at("", str(child)) == ""

    def test_repo_without_a_pin_does_not_adopt_the_home_pin(
        self, unpinned_root: Path
    ) -> None:
        home = unpinned_root / "home"
        _write_pin(home, "agent: jfreeman\n")
        _write_config(home, "agent: jfreeman\n")
        repo = home / "repo"
        (repo / ".git").mkdir(parents=True)
        with patch("quarry.ethos_tree.Path.home", return_value=home):
            assert EthosConfig.agent_handle_at(str(repo)) == ""

    def test_scratch_directory_under_home_does_not_adopt_the_home_pin(
        self, unpinned_root: Path
    ) -> None:
        """No repository at all: the home ancestor is walked through, not read."""
        home = unpinned_root / "home"
        _write_pin(home, "agent: jfreeman\n")
        scratch = home / "scratch"
        scratch.mkdir(parents=True)
        with patch("quarry.ethos_tree.Path.home", return_value=home):
            assert EthosConfig.agent_handle_at(str(scratch)) == ""

    def test_the_checkout_root_pin_itself_is_read(self, unpinned_root: Path) -> None:
        _write_pin(unpinned_root, "agent: claude\n")
        deep = unpinned_root / "src" / "quarry"
        deep.mkdir(parents=True)
        assert EthosConfig.agent_handle_at(str(deep)) == "claude"


def _vendor_identity(root: Path, handle: str) -> None:
    identities = root / ".punt-labs" / "ethos" / "identities"
    identities.mkdir(parents=True, exist_ok=True)
    (identities / f"{handle}.yaml").write_text(f"handle: {handle}\n")


class TestSubagentHandleAt:
    """``agent_type`` attributes a subagent only when it names an identity."""

    def test_registered_agent_type_is_the_handle(self, tmp_path: Path) -> None:
        _vendor_identity(tmp_path, "rmh")
        _write_pin(tmp_path, "agent: claude\n")
        assert EthosConfig.subagent_handle_at("rmh", str(tmp_path)) == "rmh"

    def test_unregistered_agent_type_is_unattributed(self, tmp_path: Path) -> None:
        _vendor_identity(tmp_path, "rmh")
        _write_pin(tmp_path, "agent: claude\n")
        # Never the leader's pin: the review is not the leader's memory.
        assert EthosConfig.subagent_handle_at("general-purpose", str(tmp_path)) == ""

    def test_invalid_handle_shape_is_unattributed(self, tmp_path: Path) -> None:
        assert EthosConfig.subagent_handle_at("../etc", str(tmp_path)) == ""

    def test_absent_agent_type_falls_back_to_the_pin(self, tmp_path: Path) -> None:
        _write_pin(tmp_path, "agent: claude\n")
        assert EthosConfig.subagent_handle_at("", str(tmp_path)) == "claude"

    def test_identity_found_from_a_subdirectory(self, tmp_path: Path) -> None:
        _vendor_identity(tmp_path, "kpz")
        deep = tmp_path / "src" / "pkg"
        deep.mkdir(parents=True)
        assert EthosConfig.subagent_handle_at("kpz", str(deep)) == "kpz"
