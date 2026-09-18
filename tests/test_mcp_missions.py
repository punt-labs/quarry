"""Behaviour of :class:`quarry.mcp_missions.MissionTools`."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from mcp.server.fastmcp import FastMCP

from quarry.client import QuarryClient, QuarryConnectionError
from quarry.mcp_missions import MissionTools
from quarry.mission_sync_types import MissionSyncOutcome
from tests.mission_fixtures import repo_with_missions

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _down() -> QuarryClient:
    raise QuarryConnectionError("quarryd is not running", "127.0.0.1")


class TestRegister:
    def test_registers_missions_sync(self) -> None:
        server = FastMCP("t")
        MissionTools(connect=_down).register(server)
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert names == {"missions_sync"}

    def test_tool_has_an_occasion_opener(self) -> None:
        doc = MissionTools.missions_sync.__doc__ or ""
        assert doc.startswith("Use after ``ethos mission close``")


class TestBoundary:
    def test_daemon_down_returns_an_error_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(repo_with_missions(tmp_path / "quarry"))
        result = MissionTools(connect=_down).missions_sync()
        assert result.startswith("Error:")
        assert "not running" in result

    def test_direct_and_registered_calls_share_the_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(repo_with_missions(tmp_path / "quarry"))
        server = FastMCP("t")
        tools = MissionTools(connect=_down)
        tools.register(server)
        direct = tools.missions_sync()
        registered = asyncio.run(server.call_tool("missions_sync", {}))
        assert direct in str(registered)


class TestOptions:
    def test_options_reach_the_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = repo_with_missions(tmp_path / "quarry")
        monkeypatch.chdir(repo)
        outcome = MissionSyncOutcome(("mission-quarry-m-1-r1",), (), (), dry_run=True)
        client = MagicMock(spec=QuarryClient)
        with patch(
            "quarry.mcp_missions.MissionMemorySync.for_repo", return_value=outcome
        ) as sync:
            text = MissionTools(connect=lambda: client).missions_sync(
                mission="m-1", dry_run=True, force=True
            )
        assert "would file 1" in text
        assert sync.call_args[0][0] == repo
        options = sync.call_args[0][2]
        assert (options.mission_id, options.dry_run, options.force) == (
            "m-1",
            True,
            True,
        )
