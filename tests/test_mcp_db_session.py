"""Exercise MCP database selection without starting the transport."""

from __future__ import annotations

import pytest

from quarry.db_pointer import DatabaseSelection
from quarry.mcp_db_session import McpDatabaseSession


def test_clear_startup_override_falls_back_to_persisted() -> None:
    selection = DatabaseSelection()
    selection.persist("saved")
    session = McpDatabaseSession(selection)
    session.select_startup("startup")
    assert selection.active() == "startup"

    session.select_startup(None)

    assert selection.active() == "saved"


def test_switch_to_default_does_not_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUARRY_URL", "wss://127.0.0.1:8420")
    selection = DatabaseSelection()
    selection.persist("saved")
    session = McpDatabaseSession(selection)
    session.select_startup("startup")

    result = session.switch_local("default")

    assert "Switched startup → default" in result
    assert selection.active() == "default"
    assert selection.persisted() == "saved"


def test_remote_switch_preserves_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUARRY_URL", "wss://remote.example.test:8420")
    selection = DatabaseSelection()
    session = McpDatabaseSession(selection)
    session.select_startup("startup")

    result = session.switch_local("default")

    assert result.startswith("Error: no local switch")
    assert selection.active() == "startup"


def test_invalid_switch_preserves_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUARRY_URL", "wss://127.0.0.1:8420")
    selection = DatabaseSelection()
    session = McpDatabaseSession(selection)
    session.select_startup("startup")

    with pytest.raises(ValueError, match="Invalid database name"):
        session.switch_local("../invalid")

    assert selection.active() == "startup"
