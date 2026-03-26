"""Tests for the hooks dispatcher and handlers."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from quarry.__main__ import app
from quarry._stdlib import HookConfig, load_hook_config, read_hook_stdin
from quarry.hooks import (
    _extract_transcript_text,
    _extract_url,
    _extract_web_fetch_content,
    _find_registration,
    _unique_collection_name,
    handle_post_web_fetch,
    handle_pre_compact,
    handle_session_start,
)
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


# ---------------------------------------------------------------------------
# Hook configuration tests
# ---------------------------------------------------------------------------


class TestLoadHookConfig:
    def test_defaults_when_file_missing(self, tmp_path: Path) -> None:
        config = load_hook_config(str(tmp_path))
        assert config == HookConfig()
        assert config.session_sync is True
        assert config.web_fetch is True
        assert config.compaction is True

    def test_disables_session_sync(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  session_sync: false\n---\n"
        )
        config = load_hook_config(str(tmp_path))
        assert config.session_sync is False
        assert config.web_fetch is True
        assert config.compaction is True

    def test_disables_web_fetch(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  web_fetch: false\n---\n"
        )
        config = load_hook_config(str(tmp_path))
        assert config.session_sync is True
        assert config.web_fetch is False

    def test_disables_compaction(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  compaction: false\n---\n"
        )
        config = load_hook_config(str(tmp_path))
        assert config.compaction is False

    def test_disables_all(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n"
            "  session_sync: false\n"
            "  web_fetch: false\n"
            "  compaction: false\n"
            "---\n"
        )
        config = load_hook_config(str(tmp_path))
        assert config.session_sync is False
        assert config.web_fetch is False
        assert config.compaction is False

    def test_invalid_yaml_returns_defaults(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text("---\n: : :\n---\n")
        config = load_hook_config(str(tmp_path))
        assert config == HookConfig()

    def test_no_frontmatter_returns_defaults(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text("Just markdown, no frontmatter.")
        config = load_hook_config(str(tmp_path))
        assert config == HookConfig()

    def test_missing_auto_capture_returns_defaults(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text("---\nother_key: value\n---\n")
        config = load_hook_config(str(tmp_path))
        assert config == HookConfig()

    def test_non_dict_auto_capture_returns_defaults(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text("---\nauto_capture: just a string\n---\n")
        config = load_hook_config(str(tmp_path))
        assert config == HookConfig()

    def test_markdown_content_after_frontmatter(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  web_fetch: false\n---\n"
            "# Notes\nSome project notes here.\n"
        )
        config = load_hook_config(str(tmp_path))
        assert config.web_fetch is False

    def test_yaml_alias_no_disables(self, tmp_path: Path) -> None:
        """YAML boolean alias 'no' should disable the hook."""
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  web_fetch: no\n---\n"
        )
        config = load_hook_config(str(tmp_path))
        assert config.web_fetch is False

    def test_yaml_alias_off_disables(self, tmp_path: Path) -> None:
        """YAML boolean alias 'off' should disable the hook."""
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  session_sync: off\n---\n"
        )
        config = load_hook_config(str(tmp_path))
        assert config.session_sync is False

    def test_yaml_alias_yes_enables(self, tmp_path: Path) -> None:
        """YAML boolean alias 'yes' should enable the hook."""
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  web_fetch: yes\n---\n"
        )
        config = load_hook_config(str(tmp_path))
        assert config.web_fetch is True

    def test_inline_comment_stripped(self, tmp_path: Path) -> None:
        """Inline YAML comments should not break boolean parsing."""
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  web_fetch: false # disabled for this project\n---\n"
        )
        config = load_hook_config(str(tmp_path))
        assert config.web_fetch is False

    def test_unrecognized_value_fails_closed(self, tmp_path: Path) -> None:
        """Unrecognized boolean value for a present key should fail closed."""
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  web_fetch: nope\n---\n"
        )
        config = load_hook_config(str(tmp_path))
        assert config.web_fetch is False

    def test_blank_lines_in_auto_capture_block(self, tmp_path: Path) -> None:
        """Blank lines within auto_capture block should not terminate parsing."""
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  session_sync: false\n\n  web_fetch: false\n---\n"
        )
        config = load_hook_config(str(tmp_path))
        assert config.session_sync is False
        assert config.web_fetch is False

    def test_comment_lines_in_auto_capture_block(self, tmp_path: Path) -> None:
        """Indented comment lines should not terminate parsing."""
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  session_sync: false\n"
            "  # disable web fetch too\n  web_fetch: false\n---\n"
        )
        config = load_hook_config(str(tmp_path))
        assert config.session_sync is False
        assert config.web_fetch is False


# ---------------------------------------------------------------------------
# _sync_in_background tests
# ---------------------------------------------------------------------------


class TestSyncInBackground:
    def test_returns_true_on_success(self, tmp_path: Path) -> None:
        import subprocess as _subprocess

        from quarry.hooks import _sync_in_background

        mock_proc = MagicMock()
        mock_proc.pid = 99999
        lockfile = tmp_path / "sync.pid"
        with (
            patch.object(_subprocess, "Popen", return_value=mock_proc),
            patch("quarry.hooks._is_sync_running", return_value=False),
            patch("quarry.hooks._sync_lockfile", return_value=lockfile),
        ):
            assert _sync_in_background() is True
            assert lockfile.exists()
            assert lockfile.read_text() == "99999"

    def test_returns_false_on_oserror(self, tmp_path: Path) -> None:
        import subprocess as _subprocess

        from quarry.hooks import _sync_in_background

        lockfile = tmp_path / "sync.pid"
        with (
            patch.object(_subprocess, "Popen", side_effect=OSError("No such file")),
            patch("quarry.hooks._is_sync_running", return_value=False),
            patch("quarry.hooks._sync_lockfile", return_value=lockfile),
        ):
            assert _sync_in_background() is False
            assert not lockfile.exists()  # Lock cleaned up on failure

    def test_skips_when_already_running(self) -> None:
        from quarry.hooks import _sync_in_background

        with patch("quarry.hooks._is_sync_running", return_value=True):
            assert _sync_in_background() is False

    def test_skips_when_lock_held(self, tmp_path: Path) -> None:
        """Atomic lock prevents TOCTOU race — second caller gets None."""
        from quarry.hooks import _sync_in_background

        lockfile = tmp_path / "sync.pid"
        lockfile.write_text("12345")  # Pre-existing lock file
        with (
            patch("quarry.hooks._is_sync_running", return_value=False),
            patch("quarry.hooks._sync_lockfile", return_value=lockfile),
        ):
            assert _sync_in_background() is False

    def test_pidfile_write_failure_still_returns_true(self, tmp_path: Path) -> None:
        """If Popen succeeds but PID write fails, sync is running — return True."""
        import subprocess as _subprocess

        from quarry.hooks import _sync_in_background

        lockfile = tmp_path / "sync.pid"
        mock_proc = MagicMock()
        mock_proc.pid = 99999

        # Create lock atomically, then make os.write fail.
        with (
            patch.object(_subprocess, "Popen", return_value=mock_proc),
            patch("quarry.hooks._is_sync_running", return_value=False),
            patch("quarry.hooks._sync_lockfile", return_value=lockfile),
            patch("os.write", side_effect=OSError("disk full")),
        ):
            assert _sync_in_background() is True  # Sync launched despite write failure


class TestIsSyncRunning:
    def test_no_pidfile_returns_false(self, tmp_path: Path) -> None:
        from quarry.hooks import _is_sync_running

        with patch("quarry.hooks._sync_lockfile", return_value=tmp_path / "sync.pid"):
            assert _is_sync_running() is False

    def test_stale_pid_returns_false(self, tmp_path: Path) -> None:
        from quarry.hooks import _is_sync_running

        pidfile = tmp_path / "sync.pid"
        pidfile.write_text("999999999")  # PID that doesn't exist
        with patch("quarry.hooks._sync_lockfile", return_value=pidfile):
            assert _is_sync_running() is False
            assert not pidfile.exists()  # Stale file cleaned up

    def test_live_pid_returns_true(self, tmp_path: Path) -> None:
        import os

        from quarry.hooks import _is_sync_running

        pidfile = tmp_path / "sync.pid"
        pidfile.write_text(str(os.getpid()))  # Current process — definitely alive
        with patch("quarry.hooks._sync_lockfile", return_value=pidfile):
            assert _is_sync_running() is True

    def test_eperm_treated_as_running(self, tmp_path: Path) -> None:
        """PermissionError (EPERM) means process exists but not ours."""
        from quarry.hooks import _is_sync_running

        pidfile = tmp_path / "sync.pid"
        pidfile.write_text("1")  # PID 1 (init) — will get EPERM
        with (
            patch("quarry.hooks._sync_lockfile", return_value=pidfile),
            patch("os.kill", side_effect=PermissionError("EPERM")),
        ):
            assert _is_sync_running() is True
            assert pidfile.exists()  # Not cleaned up — process is alive

    def test_negative_pid_treated_as_stale(self, tmp_path: Path) -> None:
        from quarry.hooks import _is_sync_running

        pidfile = tmp_path / "sync.pid"
        pidfile.write_text("-1")
        with patch("quarry.hooks._sync_lockfile", return_value=pidfile):
            assert _is_sync_running() is False
            assert not pidfile.exists()


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

    def test_registers_and_launches_background_sync(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        with (
            patch("quarry.hooks._resolve_settings", return_value=settings),
            patch("quarry.hooks._sync_in_background", return_value=True) as mock_sync,
        ):
            result = handle_session_start({"cwd": str(project)})

        mock_sync.assert_called_once()

        assert "hookSpecificOutput" in result
        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        assert output["hookEventName"] == "SessionStart"
        ctx = str(output["additionalContext"])
        assert "myproject" in ctx
        assert "Background sync in progress." in ctx

        # Verify it was registered in the registry.
        conn = open_registry(settings.registry_path)
        regs = list_registrations(conn)
        conn.close()
        assert len(regs) == 1
        assert regs[0].collection == "myproject"

    def test_context_reflects_sync_failure(self, tmp_path: Path) -> None:
        project = tmp_path / "failproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        with (
            patch("quarry.hooks._resolve_settings", return_value=settings),
            patch("quarry.hooks._sync_in_background", return_value=False),
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "Background sync skipped" in ctx

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

        with (
            patch("quarry.hooks._resolve_settings", return_value=settings),
            patch("quarry.hooks._sync_in_background") as mock_sync,
        ):
            result = handle_session_start({"cwd": str(project)})

        mock_sync.assert_called_once()

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

        with (
            patch("quarry.hooks._resolve_settings", return_value=settings),
            patch("quarry.hooks._sync_in_background"),
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "quarry MCP" in ctx

    def test_disabled_by_config(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        config_dir = project / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  session_sync: false\n---\n"
        )
        result = handle_session_start({"cwd": str(project)})
        assert result == {}

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

        with (
            patch("quarry.hooks._resolve_settings", return_value=settings),
            patch("quarry.hooks._sync_in_background"),
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


class TestExtractUrl:
    def test_extracts_url_from_tool_input(self) -> None:
        payload: dict[str, object] = {"tool_input": {"url": "https://example.com/docs"}}
        assert _extract_url(payload) == "https://example.com/docs"

    def test_returns_none_for_missing_tool_input(self) -> None:
        assert _extract_url({}) is None

    def test_returns_none_for_non_dict_tool_input(self) -> None:
        assert _extract_url({"tool_input": "not a dict"}) is None

    def test_returns_none_for_non_http_url(self) -> None:
        payload: dict[str, object] = {"tool_input": {"url": "ftp://x.com"}}
        assert _extract_url(payload) is None

    def test_returns_none_for_missing_url(self) -> None:
        payload: dict[str, object] = {"tool_input": {"other": "value"}}
        assert _extract_url(payload) is None


class TestExtractWebFetchContent:
    def test_extracts_from_json_result_field(self) -> None:
        payload: dict[str, object] = {
            "tool_response": json.dumps({"result": "<html>Hello</html>"}),
        }
        assert _extract_web_fetch_content(payload) == "<html>Hello</html>"

    def test_extracts_from_json_string(self) -> None:
        payload: dict[str, object] = {
            "tool_response": json.dumps("Plain text content"),
        }
        assert _extract_web_fetch_content(payload) == "Plain text content"

    def test_returns_none_for_missing_tool_response(self) -> None:
        assert _extract_web_fetch_content({}) is None

    def test_returns_none_for_non_string_tool_response(self) -> None:
        assert _extract_web_fetch_content({"tool_response": 42}) is None

    def test_returns_none_for_invalid_json(self) -> None:
        assert _extract_web_fetch_content({"tool_response": "not json{{"}) is None

    def test_returns_none_for_empty_result(self) -> None:
        payload: dict[str, object] = {
            "tool_response": json.dumps({"result": "  "}),
        }
        assert _extract_web_fetch_content(payload) is None

    def test_returns_none_for_empty_string(self) -> None:
        payload: dict[str, object] = {
            "tool_response": json.dumps("   "),
        }
        assert _extract_web_fetch_content(payload) is None


class TestHandlePostWebFetch:
    def test_no_url_returns_empty(self) -> None:
        result = handle_post_web_fetch({})
        assert result == {}

    def test_disabled_by_config(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        config_dir = project / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  web_fetch: false\n---\n"
        )
        payload: dict[str, object] = {
            "cwd": str(project),
            "tool_input": {"url": "https://example.com/page"},
        }
        with patch("quarry.pipeline.ingest_content") as mock_ingest:
            result = handle_post_web_fetch(payload)
        assert result == {}
        mock_ingest.assert_not_called()

    def test_ingests_content_from_tool_response(self) -> None:
        """Prefers already-fetched content from tool_response (no re-fetch)."""
        from quarry.models import PageContent, PageType

        payload: dict[str, object] = {
            "tool_input": {"url": "https://example.com/page"},
            "tool_response": json.dumps({"result": "<html>Page content</html>"}),
        }
        mock_ingest_result = {
            "document_name": "https://example.com/page",
            "collection": "web-captures",
            "chunks": 5,
        }
        mock_pages = [
            PageContent(
                text="Page content",
                page_number=1,
                total_pages=1,
                page_type=PageType.SECTION,
                document_name="https://example.com/page",
                document_path="https://example.com/page",
            )
        ]

        with (
            patch(
                "quarry.hooks._resolve_settings",
                return_value=MagicMock(),
            ),
            patch("quarry.database.get_db", return_value=MagicMock()),
            patch("quarry.hooks._is_already_ingested", return_value=False),
            patch(
                "quarry.html_processor.process_html_text",
                return_value=mock_pages,
            ),
            patch(
                "quarry.pipeline.ingest_content",
                return_value=mock_ingest_result,
            ) as mock_content,
            patch("quarry.pipeline.ingest_url") as mock_url,
        ):
            result = handle_post_web_fetch(payload)

        assert result == {}
        mock_content.assert_called_once()
        call_args = mock_content.call_args
        assert call_args[0][0] == "Page content"
        assert call_args[0][1] == "https://example.com/page"
        assert call_args[1]["collection"] == "web-captures"
        assert call_args[1]["format_hint"] == "markdown"
        mock_url.assert_not_called()

    def test_falls_back_to_ingest_url_without_tool_response(self) -> None:
        """Falls back to ingest_url when tool_response is absent."""
        payload: dict[str, object] = {
            "tool_input": {"url": "https://example.com/page"},
        }
        mock_ingest_result = {
            "document_name": "https://example.com/page",
            "collection": "web-captures",
            "chunks": 5,
        }

        with (
            patch(
                "quarry.hooks._resolve_settings",
                return_value=MagicMock(),
            ),
            patch("quarry.database.get_db", return_value=MagicMock()),
            patch("quarry.hooks._is_already_ingested", return_value=False),
            patch(
                "quarry.pipeline.ingest_url",
                return_value=mock_ingest_result,
            ) as mock_url,
            patch("quarry.pipeline.ingest_content") as mock_content,
        ):
            result = handle_post_web_fetch(payload)

        assert result == {}
        mock_url.assert_called_once()
        assert mock_url.call_args[0][0] == "https://example.com/page"
        assert mock_url.call_args[1]["collection"] == "web-captures"
        mock_content.assert_not_called()

    def test_falls_back_when_html_is_boilerplate(self) -> None:
        """Falls back to ingest_url when tool_response has no extractable text."""
        payload: dict[str, object] = {
            "tool_input": {"url": "https://example.com/page"},
            "tool_response": json.dumps({"result": "<nav>Menu</nav>"}),
        }
        mock_ingest_result = {
            "document_name": "https://example.com/page",
            "collection": "web-captures",
            "chunks": 3,
        }

        with (
            patch(
                "quarry.hooks._resolve_settings",
                return_value=MagicMock(),
            ),
            patch("quarry.database.get_db", return_value=MagicMock()),
            patch("quarry.hooks._is_already_ingested", return_value=False),
            patch("quarry.html_processor.process_html_text", return_value=[]),
            patch(
                "quarry.pipeline.ingest_url",
                return_value=mock_ingest_result,
            ) as mock_url,
        ):
            result = handle_post_web_fetch(payload)

        assert result == {}
        mock_url.assert_called_once()

    def test_skips_already_ingested_url(self) -> None:
        payload: dict[str, object] = {"tool_input": {"url": "https://example.com/old"}}

        with (
            patch(
                "quarry.hooks._resolve_settings",
                return_value=MagicMock(),
            ),
            patch("quarry.database.get_db", return_value=MagicMock()),
            patch("quarry.hooks._is_already_ingested", return_value=True),
            patch("quarry.pipeline.ingest_url") as mock_ingest,
        ):
            result = handle_post_web_fetch(payload)

        assert result == {}
        mock_ingest.assert_not_called()


class TestExtractTranscriptText:
    def _write_transcript(self, path: Path, records: list[dict[str, object]]) -> None:
        lines = [json.dumps(r) for r in records]
        path.write_text("\n".join(lines))

    def test_extracts_user_and_assistant_text(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        self._write_transcript(
            transcript,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Hello"}],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Hi there"}],
                    },
                },
            ],
        )
        text = _extract_transcript_text(str(transcript))
        assert "[user] Hello" in text
        assert "[assistant] Hi there" in text

    def test_skips_non_conversation_records(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        self._write_transcript(
            transcript,
            [
                {"type": "file-history-snapshot", "snapshot": {}},
                {"type": "system", "message": {"role": "system"}},
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Real"}],
                    },
                },
            ],
        )
        text = _extract_transcript_text(str(transcript))
        assert "[user] Real" in text
        assert "snapshot" not in text
        assert "system" not in text

    def test_skips_tool_use_blocks(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        self._write_transcript(
            transcript,
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Bash"},
                            {"type": "text", "text": "Done"},
                        ],
                    },
                },
            ],
        )
        text = _extract_transcript_text(str(transcript))
        assert "[assistant] Done" in text
        assert "Bash" not in text

    def test_returns_empty_for_nonexistent_file(self) -> None:
        assert _extract_transcript_text("/nonexistent/path.jsonl") == ""

    def test_returns_empty_for_empty_file(self, tmp_path: Path) -> None:
        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("")
        assert _extract_transcript_text(str(transcript)) == ""

    def test_respects_char_limit(self, tmp_path: Path) -> None:
        transcript = tmp_path / "big.jsonl"
        # Each entry is "[user] " + 200 "a"s = 207 chars.
        # 5000 entries = 1,035,000 chars of content alone.
        big_text = "a" * 200
        records = [
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": big_text}],
                    },
                }
            )
            for _ in range(5000)
        ]
        transcript.write_text("\n".join(records))

        from quarry.hooks import _MAX_TRANSCRIPT_CHARS

        text = _extract_transcript_text(str(transcript))
        # Total includes "\n\n" separators between entries, so allow
        # a small margin above the content limit.
        assert len(text) < _MAX_TRANSCRIPT_CHARS * 1.02

    def test_returns_empty_for_unreadable_file(self, tmp_path: Path) -> None:
        transcript = tmp_path / "binary.jsonl"
        transcript.write_bytes(b"\x80\x81\x82\xff\xfe")
        assert _extract_transcript_text(str(transcript)) == ""


class TestHandlePreCompact:
    def test_no_transcript_returns_empty(self) -> None:
        result = handle_pre_compact({})
        assert result == {}

    def test_disabled_by_config(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        config_dir = project / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  compaction: false\n---\n"
        )
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Hello"}],
                    },
                }
            )
        )
        payload: dict[str, object] = {
            "cwd": str(project),
            "transcript_path": str(transcript),
            "session_id": "abc123",
        }
        with patch("quarry.pipeline.ingest_content") as mock_ingest:
            result = handle_pre_compact(payload)
        assert result == {}
        mock_ingest.assert_not_called()

    def test_no_session_id_returns_empty(self, tmp_path: Path) -> None:
        result = handle_pre_compact({"transcript_path": str(tmp_path / "t.jsonl")})
        assert result == {}

    def test_rejects_non_jsonl_transcript(self, tmp_path: Path) -> None:
        payload: dict[str, object] = {
            "transcript_path": str(tmp_path / "secrets.txt"),
            "session_id": "abc123",
        }
        result = handle_pre_compact(payload)
        assert result == {}

    def test_ingests_transcript(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Build a feature"}],
                    },
                }
            )
        )

        mock_result = {
            "document_name": "session-abc12345-20260224T120000",
            "collection": "session-notes",
            "chunks": 1,
        }

        with (
            patch(
                "quarry.hooks._resolve_settings",
                return_value=MagicMock(),
            ),
            patch("quarry.database.get_db", return_value=MagicMock()),
            patch(
                "quarry.pipeline.ingest_content",
                return_value=mock_result,
            ) as mock_ingest,
        ):
            result = handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        assert result == {}
        mock_ingest.assert_called_once()
        call_args = mock_ingest.call_args
        assert "Build a feature" in call_args[0][0]  # content
        assert call_args[0][1].startswith("session-abc12345")  # doc name
        assert call_args[1]["collection"] == "session-notes"

    def test_empty_transcript_skips_ingestion(self, tmp_path: Path) -> None:
        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("")

        with patch("quarry.pipeline.ingest_content") as mock_ingest:
            result = handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc123",
                }
            )

        assert result == {}
        mock_ingest.assert_not_called()


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


# ---------------------------------------------------------------------------
# Wiring tests — hooks.json references scripts that exist and are executable
# ---------------------------------------------------------------------------

_HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"


class TestHookWiring:
    """Verify hooks.json entries reference shell scripts that exist."""

    def test_hooks_json_is_valid(self) -> None:
        hooks_json = _HOOKS_DIR / "hooks.json"
        data = json.loads(hooks_json.read_text())
        assert "hooks" in data

    def test_all_referenced_scripts_exist(self) -> None:
        hooks_json = _HOOKS_DIR / "hooks.json"
        data = json.loads(hooks_json.read_text())
        plugin_root = _HOOKS_DIR.parent

        for entries in data["hooks"].values():
            for entry in entries:
                for hook in entry.get("hooks", []):
                    cmd = hook.get("command", "")
                    # Resolve ${CLAUDE_PLUGIN_ROOT} to the actual plugin root.
                    resolved = cmd.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))
                    script = Path(resolved)
                    assert script.is_file(), f"Missing script: {cmd}"
                    assert script.stat().st_mode & 0o111, f"Not executable: {cmd}"

    def test_session_sync_script_exists(self) -> None:
        script = _HOOKS_DIR / "session-sync.sh"
        assert script.is_file()
        assert script.stat().st_mode & 0o111

    def test_web_fetch_script_exists(self) -> None:
        script = _HOOKS_DIR / "web-fetch.sh"
        assert script.is_file()
        assert script.stat().st_mode & 0o111

    def test_pre_compact_script_exists(self) -> None:
        script = _HOOKS_DIR / "pre-compact.sh"
        assert script.is_file()
        assert script.stat().st_mode & 0o111

    def test_session_start_hook_registered(self) -> None:
        """SessionStart has entries for both plugin setup and Python sync."""
        hooks_json = _HOOKS_DIR / "hooks.json"
        data = json.loads(hooks_json.read_text())
        session_entries = data["hooks"]["SessionStart"]
        commands = [
            h["command"] for entry in session_entries for h in entry.get("hooks", [])
        ]
        assert any("session-start.sh" in c for c in commands), (
            "Missing plugin setup hook"
        )
        assert any("session-sync.sh" in c for c in commands), "Missing Python sync hook"

    def test_web_fetch_hook_registered(self) -> None:
        """PostToolUse has a WebFetch matcher entry."""
        hooks_json = _HOOKS_DIR / "hooks.json"
        data = json.loads(hooks_json.read_text())
        post_entries = data["hooks"]["PostToolUse"]
        web_fetch_entries = [e for e in post_entries if e.get("matcher") == "WebFetch"]
        assert len(web_fetch_entries) == 1
        commands = [h["command"] for h in web_fetch_entries[0]["hooks"]]
        assert any("web-fetch.sh" in c for c in commands)

    def test_pre_compact_hook_registered(self) -> None:
        """PreCompact event is registered in hooks.json."""
        hooks_json = _HOOKS_DIR / "hooks.json"
        data = json.loads(hooks_json.read_text())
        assert "PreCompact" in data["hooks"]
        pre_entries = data["hooks"]["PreCompact"]
        commands = [
            h["command"] for entry in pre_entries for h in entry.get("hooks", [])
        ]
        assert any("pre-compact.sh" in c for c in commands)


class TestReadHookStdin:
    """Verify read_hook_stdin doesn't block on open pipes (DES-027)."""

    def test_empty_stdin_returns_empty(self) -> None:
        """EOF with no data returns empty string."""
        r_fd, w_fd = os.pipe()
        os.close(w_fd)
        r = os.fdopen(r_fd, "r")
        with patch.object(sys, "stdin", r):
            result = read_hook_stdin()
        r.close()
        assert result == ""

    def test_valid_json_parsed(self) -> None:
        """Valid JSON on stdin is read and returned."""
        r_fd, w_fd = os.pipe()
        payload = json.dumps({"cwd": "/tmp/test"})
        os.write(w_fd, payload.encode())
        os.close(w_fd)
        r = os.fdopen(r_fd, "r")
        with patch.object(sys, "stdin", r):
            result = read_hook_stdin()
        r.close()
        assert result == payload

    def test_no_eof_does_not_hang(self) -> None:
        """Stdin with data but no EOF returns data without blocking.

        Regression test for the session resume hang: Claude Code pipes
        data but may not close the pipe.
        """
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b'{"cwd": "/tmp/test"}\n')
        # Do NOT close w_fd — simulates open pipe without EOF.
        r = os.fdopen(r_fd, "r")
        try:
            with patch.object(sys, "stdin", r):
                result = read_hook_stdin()
        finally:
            r.close()
            os.close(w_fd)
        assert result == '{"cwd": "/tmp/test"}\n'

    def test_no_data_no_eof_returns_empty(self) -> None:
        """Open pipe with no data returns empty without blocking."""
        r_fd, w_fd = os.pipe()
        r = os.fdopen(r_fd, "r")
        try:
            with patch.object(sys, "stdin", r):
                result = read_hook_stdin()
        finally:
            r.close()
            os.close(w_fd)
        assert result == ""
