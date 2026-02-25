"""Tests for the hooks dispatcher and handlers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.hooks import (
    _find_registration,
    _format_context,
    _unique_collection_name,
    handle_post_web_fetch,
    handle_pre_compact,
    handle_session_start,
)
from quarry.sync import SyncResult
from quarry.sync_registry import (
    DirectoryRegistration,
    list_registrations,
    open_registry,
    register_directory,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


class TestFindRegistration:
    def test_finds_matching_directory(self) -> None:
        regs = [
            DirectoryRegistration("/a", "col-a", "2026-01-01"),
            DirectoryRegistration("/b", "col-b", "2026-01-01"),
        ]
        assert _find_registration(regs, "/b") == regs[1]

    def test_returns_none_when_not_found(self) -> None:
        regs = [DirectoryRegistration("/a", "col-a", "2026-01-01")]
        assert _find_registration(regs, "/z") is None

    def test_empty_list(self) -> None:
        assert _find_registration([], "/a") is None


class TestUniqueCollectionName:
    def test_uses_leaf_name_when_available(self, tmp_path: Path) -> None:
        conn = open_registry(tmp_path / "r.db")
        project = tmp_path / "myproject"
        project.mkdir()
        assert _unique_collection_name(conn, project) == "myproject"
        conn.close()

    def test_disambiguates_with_parent(self, tmp_path: Path) -> None:
        conn = open_registry(tmp_path / "r.db")
        # Register a different directory with the same leaf name.
        other = tmp_path / "other" / "myproject"
        other.mkdir(parents=True)
        register_directory(conn, other, "myproject")

        project = tmp_path / "mine" / "myproject"
        project.mkdir(parents=True)
        name = _unique_collection_name(conn, project)
        assert name == "myproject-mine"
        conn.close()

    def test_falls_back_to_hash_on_double_collision(self, tmp_path: Path) -> None:
        conn = open_registry(tmp_path / "r.db")
        # Occupy both "myproject" and "myproject-mine".
        d1 = tmp_path / "a" / "myproject"
        d1.mkdir(parents=True)
        register_directory(conn, d1, "myproject")

        d2 = tmp_path / "b" / "myproject"
        d2.mkdir(parents=True)
        register_directory(conn, d2, "myproject-mine")

        project = tmp_path / "mine" / "myproject"
        project.mkdir(parents=True)
        name = _unique_collection_name(conn, project)
        assert name.startswith("myproject-")
        assert len(name) == len("myproject-") + 8  # 8-char hash
        conn.close()


class TestFormatContext:
    def test_includes_collection_and_directory(self) -> None:
        result = SyncResult("col", ingested=1, deleted=0, skipped=5, failed=0)
        ctx = _format_context("col", "/my/project", result)
        assert 'Collection: "col"' in ctx
        assert "/my/project" in ctx

    def test_shows_sync_stats(self) -> None:
        result = SyncResult("col", ingested=3, deleted=1, skipped=10, failed=2)
        ctx = _format_context("col", "/p", result)
        assert "3 ingested" in ctx
        assert "1 deleted" in ctx
        assert "10 unchanged" in ctx
        assert "2 failed" in ctx

    def test_empty_collection_message(self) -> None:
        result = SyncResult("col", ingested=0, deleted=0, skipped=0, failed=0)
        ctx = _format_context("col", "/p", result)
        assert "empty" in ctx


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------


class TestHandleSessionStart:
    def test_no_cwd_returns_empty(self) -> None:
        result = handle_session_start({})
        assert result == {}

    def test_empty_cwd_returns_empty(self) -> None:
        result = handle_session_start({"cwd": ""})
        assert result == {}

    def test_nonexistent_cwd_returns_empty(self) -> None:
        result = handle_session_start({"cwd": "/nonexistent/path/xyz123"})
        assert result == {}

    def test_registers_and_syncs_new_directory(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "hello.txt").write_text("world")

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        mock_result = SyncResult(
            "myproject", ingested=1, deleted=0, skipped=0, failed=0
        )

        with (
            patch("quarry.hooks._resolve_settings", return_value=settings),
            patch("quarry.hooks.sync_collection", return_value=mock_result),
        ):
            result = handle_session_start({"cwd": str(project)})

        assert "hookSpecificOutput" in result
        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        assert output["hookEventName"] == "SessionStart"
        ctx = str(output["additionalContext"])
        assert "myproject" in ctx
        assert "1 ingested" in ctx

        # Verify it was registered in the registry.
        conn = open_registry(settings.registry_path)
        regs = list_registrations(conn)
        conn.close()
        assert len(regs) == 1
        assert regs[0].collection == "myproject"

    def test_skips_registration_when_already_registered(self, tmp_path: Path) -> None:
        project = tmp_path / "existing"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        # Pre-register the directory.
        conn = open_registry(settings.registry_path)
        register_directory(conn, project, "custom-name")
        conn.close()

        mock_result = SyncResult(
            "custom-name", ingested=0, deleted=0, skipped=3, failed=0
        )

        with (
            patch("quarry.hooks._resolve_settings", return_value=settings),
            patch(
                "quarry.hooks.sync_collection", return_value=mock_result
            ) as mock_sync,
        ):
            result = handle_session_start({"cwd": str(project)})

        # Uses existing collection name, not directory name.
        mock_sync.assert_called_once()
        call_args = mock_sync.call_args
        assert call_args[0][1] == "custom-name"  # collection arg

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "custom-name" in ctx

    def test_returns_additional_context_with_mcp_guidance(self, tmp_path: Path) -> None:
        project = tmp_path / "repo"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        mock_result = SyncResult("repo", ingested=0, deleted=0, skipped=10, failed=0)

        with (
            patch("quarry.hooks._resolve_settings", return_value=settings),
            patch("quarry.hooks.sync_collection", return_value=mock_result),
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "search_documents" in ctx or "quarry MCP" in ctx

    def test_disambiguates_on_collection_name_collision(self, tmp_path: Path) -> None:
        """Two directories with the same leaf name get distinct collections."""
        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        # Pre-register a different directory under "myproject".
        other = tmp_path / "other" / "myproject"
        other.mkdir(parents=True)
        conn = open_registry(settings.registry_path)
        register_directory(conn, other, "myproject")
        conn.close()

        # Now the hook registers a new directory also named "myproject".
        project = tmp_path / "mine" / "myproject"
        project.mkdir(parents=True)

        mock_result = SyncResult(
            "myproject-mine", ingested=2, deleted=0, skipped=0, failed=0
        )

        with (
            patch("quarry.hooks._resolve_settings", return_value=settings),
            patch("quarry.hooks.sync_collection", return_value=mock_result),
        ):
            result = handle_session_start({"cwd": str(project)})

        assert "hookSpecificOutput" in result
        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "myproject-mine" in ctx

        # Verify both registrations exist.
        conn = open_registry(settings.registry_path)
        regs = list_registrations(conn)
        conn.close()
        assert len(regs) == 2
        collections = {r.collection for r in regs}
        assert "myproject" in collections
        assert "myproject-mine" in collections


class TestOtherHandlers:
    """post-web-fetch and pre-compact remain stubs."""

    def test_post_web_fetch_returns_dict(self) -> None:
        result = handle_post_web_fetch({})
        assert isinstance(result, dict)

    def test_pre_compact_returns_dict(self) -> None:
        result = handle_pre_compact({})
        assert isinstance(result, dict)

    def test_handlers_accept_arbitrary_payload(self) -> None:
        payload: dict[str, object] = {"tool_name": "WebFetch", "url": "https://x.com"}
        result = handle_post_web_fetch(payload)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# CLI dispatcher tests
# ---------------------------------------------------------------------------


class TestHookCLI:
    """The CLI dispatcher reads stdin JSON, calls the handler, writes stdout."""

    def test_session_start_no_cwd_returns_empty_json(self) -> None:
        result = runner.invoke(app, ["hooks", "session-start"], input="")
        assert result.exit_code == 0
        assert json.loads(result.output) == {}

    def test_post_web_fetch_accepts_json_stdin(self) -> None:
        payload = json.dumps({"tool_input": {"url": "https://example.com"}})
        result = runner.invoke(app, ["hooks", "post-web-fetch"], input=payload)
        assert result.exit_code == 0
        assert json.loads(result.output) == {}

    def test_pre_compact_accepts_empty_stdin(self) -> None:
        result = runner.invoke(app, ["hooks", "pre-compact"], input="")
        assert result.exit_code == 0
        assert json.loads(result.output) == {}

    def test_hooks_help(self) -> None:
        result = runner.invoke(app, ["hooks", "--help"])
        assert result.exit_code == 0
        assert "session-start" in result.output
        assert "post-web-fetch" in result.output
        assert "pre-compact" in result.output

    def test_invalid_json_is_fail_open(self) -> None:
        result = runner.invoke(app, ["hooks", "session-start"], input="not json{{{")
        assert result.exit_code == 0
        assert json.loads(result.output) == {}
