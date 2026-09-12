"""Behavior of :meth:`quarry.ethos_handle.EthosConfig.agent_handle_at`.

Fresh module with one caller today (``doctor_memory``); ``hooks.py`` still
carries its own inline walker that a follow-up unit migrates onto this helper.
Until then this suite is the sole exercise of the shared walker.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from quarry.ethos_handle import EthosConfig

if TYPE_CHECKING:
    import pytest


def _write_config(root: Path, body: str) -> None:
    """Deposit an ethos config at ``root/.punt-labs/ethos/config.yaml``."""
    config_dir = root / ".punt-labs" / "ethos"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(body)


class TestReadAgentHandle:
    def test_returns_agent_from_direct_config(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "agent: rmh\n")
        assert EthosConfig.agent_handle_at(str(tmp_path)) == "rmh"

    def test_walks_up_to_ancestor_config(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "agent: kpz\n")
        deep = tmp_path / "src" / "quarry" / "retrieval"
        deep.mkdir(parents=True)
        assert EthosConfig.agent_handle_at(str(deep)) == "kpz"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        # No config anywhere on the walk — the walker must not raise, since
        # the empty string is the documented "no identity here" signal.
        assert EthosConfig.agent_handle_at(str(tmp_path)) == ""

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
