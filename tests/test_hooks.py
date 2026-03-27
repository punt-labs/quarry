"""Tests for the hooks dispatcher and handlers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from quarry.__main__ import app
from quarry._stdlib import HookConfig, load_hook_config, read_hook_stdin
from quarry.hooks import (
    _collection_for_cwd,
    _extract_message_text,
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


class TestCollectionForCwd:
    def test_returns_collection_for_exact_match(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"

        conn = open_registry(settings.registry_path)
        register_directory(conn, project, "myproject")
        conn.close()

        with patch("quarry.hooks._resolve_settings", return_value=settings):
            result = _collection_for_cwd(str(project))
        assert result == "myproject"

    def test_returns_collection_for_subdirectory(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        subdir = project / "src" / "lib"
        subdir.mkdir(parents=True)
        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"

        conn = open_registry(settings.registry_path)
        register_directory(conn, project, "myproject")
        conn.close()

        with patch("quarry.hooks._resolve_settings", return_value=settings):
            result = _collection_for_cwd(str(subdir))
        assert result == "myproject"

    def test_returns_none_for_unregistered_directory(self, tmp_path: Path) -> None:
        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"

        conn = open_registry(settings.registry_path)
        conn.close()

        with patch("quarry.hooks._resolve_settings", return_value=settings):
            result = _collection_for_cwd(str(tmp_path / "unregistered"))
        assert result is None

    def test_returns_none_for_empty_cwd(self) -> None:
        assert _collection_for_cwd("") is None


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

    def test_context_includes_recall_hint(self, tmp_path: Path) -> None:
        project = tmp_path / "hintproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        with (
            patch("quarry.hooks._resolve_settings", return_value=settings),
            patch("quarry.hooks._sync_in_background", return_value=True),
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert ctx.startswith("Before researching")


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
            patch("quarry.hooks._collection_for_cwd", return_value=None),
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
            patch("quarry.hooks._collection_for_cwd", return_value=None),
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
            patch("quarry.hooks._collection_for_cwd", return_value=None),
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
            patch("quarry.hooks._collection_for_cwd", return_value=None),
            patch("quarry.pipeline.ingest_url") as mock_ingest,
        ):
            result = handle_post_web_fetch(payload)

        assert result == {}
        mock_ingest.assert_not_called()

    def test_uses_project_collection_when_registered(self) -> None:
        """Web captures go to the project collection when cwd is registered."""
        payload: dict[str, object] = {
            "cwd": "/projects/myapp",
            "tool_input": {"url": "https://example.com/docs"},
            "tool_response": json.dumps({"result": "Some docs"}),
        }
        mock_ingest_result = {
            "document_name": "https://example.com/docs",
            "collection": "myapp",
            "chunks": 3,
        }

        with (
            patch("quarry.hooks._resolve_settings", return_value=MagicMock()),
            patch("quarry.database.get_db", return_value=MagicMock()),
            patch("quarry.hooks._is_already_ingested", return_value=False),
            patch("quarry.hooks._collection_for_cwd", return_value="myapp"),
            patch("quarry.html_processor.process_html_text", return_value=[]),
            patch(
                "quarry.pipeline.ingest_url",
                return_value=mock_ingest_result,
            ) as mock_url,
        ):
            handle_post_web_fetch(payload)

        assert mock_url.call_args[1]["collection"] == "myapp"

    def test_falls_back_to_web_captures_when_unregistered(self) -> None:
        """Web captures use fallback collection when cwd has no registration."""
        payload: dict[str, object] = {
            "cwd": "/unknown/dir",
            "tool_input": {"url": "https://example.com/page"},
            "tool_response": json.dumps({"result": "Content"}),
        }
        mock_ingest_result = {
            "document_name": "https://example.com/page",
            "collection": "web-captures",
            "chunks": 2,
        }

        with (
            patch("quarry.hooks._resolve_settings", return_value=MagicMock()),
            patch("quarry.database.get_db", return_value=MagicMock()),
            patch("quarry.hooks._is_already_ingested", return_value=False),
            patch("quarry.hooks._collection_for_cwd", return_value=None),
            patch("quarry.html_processor.process_html_text", return_value=[]),
            patch(
                "quarry.pipeline.ingest_url",
                return_value=mock_ingest_result,
            ) as mock_url,
        ):
            handle_post_web_fetch(payload)

        assert mock_url.call_args[1]["collection"] == "web-captures"


class TestExtractMessageText:
    def test_extracts_short_tool_result_string(self) -> None:
        record: dict[str, object] = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": "5 passed, 0 failed",
                    }
                ],
            },
        }
        result = _extract_message_text(record)
        assert result is not None
        assert "[tool_result] 5 passed, 0 failed" in result

    def test_extracts_short_tool_result_list(self) -> None:
        record: dict[str, object] = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": [{"type": "text", "text": "No matches found"}],
                    }
                ],
            },
        }
        result = _extract_message_text(record)
        assert result is not None
        assert "[tool_result] No matches found" in result

    def test_skips_long_tool_result(self) -> None:
        long_output = "x" * 501
        record: dict[str, object] = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": long_output,
                    }
                ],
            },
        }
        result = _extract_message_text(record)
        assert result is None

    def test_skips_tool_use_blocks(self) -> None:
        record: dict[str, object] = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_abc",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    }
                ],
            },
        }
        result = _extract_message_text(record)
        assert result is None


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

    def test_front_truncation_keeps_newest(self, tmp_path: Path) -> None:
        from quarry.hooks import _MAX_TRANSCRIPT_CHARS

        transcript = tmp_path / "session.jsonl"
        # Each message is "[user] " (7 chars) + text.
        # Make msg1 large enough that msg1+msg2+msg3 > _MAX_TRANSCRIPT_CHARS
        # but msg2+msg3 fits.
        half = _MAX_TRANSCRIPT_CHARS // 2
        msg1_text = "A" * half
        msg2_text = "B" * half
        msg3_text = "C" * 50
        self._write_transcript(
            transcript,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": msg1_text}],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": msg2_text}],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": msg3_text}],
                    },
                },
            ],
        )
        text = _extract_transcript_text(str(transcript))
        # First message (AAA...) should be dropped, last two kept.
        assert "A" * 50 not in text
        assert msg2_text in text
        assert msg3_text in text

    def test_returns_empty_for_unreadable_file(self, tmp_path: Path) -> None:
        transcript = tmp_path / "binary.jsonl"
        transcript.write_bytes(b"\x80\x81\x82\xff\xfe")
        assert _extract_transcript_text(str(transcript)) == ""


def _make_transcript(tmp_path: Path, text: str = "Build a feature") -> Path:
    """Create a minimal JSONL transcript file for testing."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
    )
    return transcript


def _mock_settings() -> MagicMock:
    """Return a mock Settings with a lancedb_path attribute."""
    s = MagicMock()
    s.lancedb_path = Path("/fake/lancedb")
    return s


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
        transcript = _make_transcript(tmp_path, "Hello")
        payload: dict[str, object] = {
            "cwd": str(project),
            "transcript_path": str(transcript),
            "session_id": "abc123",
        }
        result = handle_pre_compact(payload)
        assert result == {}

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

    def test_returns_immediately_with_system_message(self, tmp_path: Path) -> None:
        """handle_pre_compact returns systemMessage with collection and doc name."""
        transcript = _make_transcript(tmp_path)

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.hooks._collection_for_cwd", return_value=None),
            patch("quarry.hooks.subprocess.Popen") as mock_popen,
        ):
            result = handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        assert "systemMessage" in result
        msg = str(result["systemMessage"])
        assert "session-abc12345-" in msg
        assert '"session-notes"' in msg
        assert "/find" in msg
        mock_popen.assert_called_once()

    def test_popen_called_with_correct_args(self, tmp_path: Path) -> None:
        """subprocess.Popen receives quarry-hook ingest-background with all args."""
        transcript = _make_transcript(tmp_path)

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.hooks._collection_for_cwd", return_value="myapp"),
            patch("quarry.hooks.subprocess.Popen") as mock_popen,
        ):
            handle_pre_compact(
                {
                    "cwd": "/projects/myapp",
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        args = mock_popen.call_args[0][0]
        assert args[0] == sys.executable
        assert args[1:3] == ["-m", "quarry._hook_entry"]
        assert args[3] == "ingest-background"
        # args[4] is the text file path
        assert args[4].endswith(".txt")
        assert "session-abc12345-" in args[5]  # document_name
        assert args[6] == "myapp"  # collection
        assert args[7] == str(Path("/fake/lancedb"))  # lancedb_path
        assert args[8] == "abc12345"  # session_prefix

        kwargs = mock_popen.call_args[1]
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL

    def test_writes_text_file_for_background(self, tmp_path: Path) -> None:
        """Extracted text is written to a temp file in sessions dir."""
        transcript = _make_transcript(tmp_path, "Important context here")

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.hooks._collection_for_cwd", return_value=None),
            patch("quarry.hooks.subprocess.Popen") as mock_popen,
        ):
            handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        text_file = Path(mock_popen.call_args[0][0][4])
        assert text_file.exists()
        assert "Important context here" in text_file.read_text()

    def test_uses_project_collection_when_registered(self, tmp_path: Path) -> None:
        """Session notes go to the project collection when cwd is registered."""
        transcript = _make_transcript(tmp_path, "Working on myapp")

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.hooks._collection_for_cwd", return_value="myapp"),
            patch("quarry.hooks.subprocess.Popen") as mock_popen,
        ):
            result = handle_pre_compact(
                {
                    "cwd": "/projects/myapp",
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        assert '"myapp"' in str(result["systemMessage"])
        assert mock_popen.call_args[0][0][6] == "myapp"

    def test_falls_back_to_session_notes_when_unregistered(
        self, tmp_path: Path
    ) -> None:
        """Session notes use fallback collection when cwd has no registration."""
        transcript = _make_transcript(tmp_path, "Some work")

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.hooks._collection_for_cwd", return_value=None),
            patch("quarry.hooks.subprocess.Popen") as mock_popen,
        ):
            handle_pre_compact(
                {
                    "cwd": "/unknown/dir",
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        assert mock_popen.call_args[0][0][6] == "session-notes"

    def test_empty_transcript_skips_ingestion(self, tmp_path: Path) -> None:
        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("")

        with patch("quarry.hooks.subprocess.Popen") as mock_popen:
            result = handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc123",
                }
            )

        assert result == {}
        mock_popen.assert_not_called()

    def test_archives_raw_jsonl(self, tmp_path: Path) -> None:
        """Raw JSONL is copied to the sessions directory."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Hello"}]}}\n'
        )

        sessions_dir = tmp_path / "home" / ".punt-labs" / "quarry" / "sessions"

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.hooks._collection_for_cwd", return_value=None),
            patch("quarry.hooks.subprocess.Popen"),
        ):
            handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        archived = list(sessions_dir.glob("session-abc12345-*.jsonl"))
        assert len(archived) == 1
        assert archived[0].read_text() == transcript.read_text()

    def test_archive_retention_deletes_old_files(self, tmp_path: Path) -> None:
        """Files older than 90 days are deleted during archival."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Hello"}]}}\n'
        )

        sessions_dir = tmp_path / "home" / ".punt-labs" / "quarry" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create an old archive (100 days ago).
        old_file = sessions_dir / "session-oldoldol-20250101T000000.jsonl"
        old_file.write_text("{}\n")
        old_mtime = old_file.stat().st_mtime - (100 * 86400)
        os.utime(old_file, (old_mtime, old_mtime))

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.hooks._collection_for_cwd", return_value=None),
            patch("quarry.hooks.subprocess.Popen"),
        ):
            handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        assert not old_file.exists()
        new_archives = list(sessions_dir.glob("session-abc12345-*.jsonl"))
        assert len(new_archives) == 1

    def test_archive_failure_does_not_prevent_capture(self, tmp_path: Path) -> None:
        """Background ingest is spawned even when archival raises an exception."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Hello"}]}}\n'
        )

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch("quarry.hooks.shutil.copy", side_effect=OSError("disk full")),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.hooks._collection_for_cwd", return_value=None),
            patch("quarry.hooks.subprocess.Popen") as mock_popen,
        ):
            handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        mock_popen.assert_called_once()

    def test_archive_deduplicates_prior_sessions(self, tmp_path: Path) -> None:
        """Prior archive files for the same session are replaced."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Hello"}]}}\n'
        )

        sessions_dir = tmp_path / "home" / ".punt-labs" / "quarry" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create a prior archive for the same session.
        prior = sessions_dir / "session-abc12345-20260224T100000.jsonl"
        prior.write_text("{}\n")

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.hooks._collection_for_cwd", return_value=None),
            patch("quarry.hooks.subprocess.Popen"),
        ):
            handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        assert not prior.exists()
        all_archives = list(sessions_dir.glob("session-abc12345-*.jsonl"))
        assert len(all_archives) == 1

    def test_archive_survives_retention_with_old_source_mtime(
        self, tmp_path: Path
    ) -> None:
        """Archive from old source transcript is not deleted by retention."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Hello"}]}}\n'
        )

        # Backdate source mtime to 95 days ago.
        old_time = transcript.stat().st_mtime - (95 * 86400)
        os.utime(transcript, (old_time, old_time))

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.hooks._collection_for_cwd", return_value=None),
            patch("quarry.hooks.subprocess.Popen"),
        ):
            handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        sessions_dir = tmp_path / "home" / ".punt-labs" / "quarry" / "sessions"
        new_archives = list(sessions_dir.glob("session-abc12345-*.jsonl"))
        assert len(new_archives) == 1, "archive should survive retention cleanup"

    def test_system_message_contains_collection_and_doc_name(
        self, tmp_path: Path
    ) -> None:
        """systemMessage includes collection, document name, and /find hint."""
        transcript = _make_transcript(tmp_path, "Confirm capture")

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.hooks._collection_for_cwd", return_value="quarry"),
            patch("quarry.hooks.subprocess.Popen"),
        ):
            result = handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        assert "systemMessage" in result
        msg = str(result["systemMessage"])
        assert "session-abc12345-" in msg
        assert '"quarry"' in msg
        assert "/find" in msg
        # No chunk count in new format.
        assert "chunks" not in msg

    def test_popen_failure_cleans_up_and_returns_warning(self, tmp_path: Path) -> None:
        """Popen OSError cleans up temp file and returns a warning systemMessage."""
        transcript = _make_transcript(tmp_path, "Will not be ingested")

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.hooks._collection_for_cwd", return_value=None),
            patch(
                "quarry.hooks.subprocess.Popen",
                side_effect=FileNotFoundError("quarry-hook"),
            ),
        ):
            result = handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        assert "systemMessage" in result
        msg = str(result["systemMessage"])
        assert "Warning" in msg
        assert "sessions/" in msg
        # Temp file should be cleaned up.
        sessions_dir = tmp_path / "home" / ".punt-labs" / "quarry" / "sessions"
        txt_files = list(sessions_dir.glob("*.txt"))
        assert txt_files == []

    def test_system_message_uses_present_tense(self, tmp_path: Path) -> None:
        """systemMessage says 'Capturing' not 'captured' (async honesty)."""
        transcript = _make_transcript(tmp_path)

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.hooks._collection_for_cwd", return_value=None),
            patch("quarry.hooks.subprocess.Popen"),
        ):
            result = handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        msg = str(result["systemMessage"])
        assert msg.startswith("Capturing")
        assert "background" in msg


class TestIngestBackground:
    """Tests for the ingest-background entry point."""

    def test_performs_dedup_and_ingestion(self, tmp_path: Path) -> None:
        """Background entry point does dedup + ingest and cleans up temp file."""
        text_file = tmp_path / "session-abc12345-20260327T120000.txt"
        text_file.write_text("Some transcript content")

        mock_result: dict[str, object] = {
            "document_name": "session-abc12345-20260327T120000",
            "collection": "session-notes",
            "chunks": 5,
        }

        existing_docs = [
            {
                "document_name": "session-abc12345-20260327T100000",
                "document_path": "",
                "collection": "session-notes",
                "total_pages": 1,
                "chunk_count": 3,
                "indexed_pages": 1,
                "ingestion_timestamp": "2026-03-27T10:00:00",
            },
        ]

        from quarry._hook_entry import _ingest_background

        with (
            patch(
                "sys.argv",
                [
                    "quarry-hook",
                    "ingest-background",
                    str(text_file),
                    "session-abc12345-20260327T120000",
                    "session-notes",
                    str(tmp_path / "lancedb"),
                    "abc12345",
                ],
            ),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=MagicMock(),
            ),
            patch(
                "quarry.config.load_settings",
                return_value=MagicMock(),
            ),
            patch(
                "quarry.database.get_db",
                return_value=MagicMock(),
            ),
            patch(
                "quarry.database.list_documents",
                return_value=existing_docs,
            ),
            patch(
                "quarry.database.delete_document",
            ) as mock_delete,
            patch(
                "quarry.pipeline.ingest_content",
                return_value=mock_result,
            ) as mock_ingest,
        ):
            _ingest_background()

        mock_delete.assert_called_once()
        mock_ingest.assert_called_once()
        call_args = mock_ingest.call_args
        assert "Some transcript content" in call_args[0][0]
        assert call_args[0][1] == "session-abc12345-20260327T120000"
        assert call_args[1]["collection"] == "session-notes"
        # Temp file cleaned up.
        assert not text_file.exists()

    def test_cleans_up_temp_file_on_ingest_failure(self, tmp_path: Path) -> None:
        """Temp file is removed even when ingestion fails."""
        text_file = tmp_path / "session-abc12345-20260327T120000.txt"
        text_file.write_text("Content")

        from quarry._hook_entry import _ingest_background

        with (
            patch(
                "sys.argv",
                [
                    "quarry-hook",
                    "ingest-background",
                    str(text_file),
                    "session-abc12345-20260327T120000",
                    "session-notes",
                    str(tmp_path / "lancedb"),
                    "abc12345",
                ],
            ),
            patch(
                "quarry.config.resolve_db_paths",
                return_value=MagicMock(),
            ),
            patch(
                "quarry.config.load_settings",
                return_value=MagicMock(),
            ),
            patch(
                "quarry.database.get_db",
                return_value=MagicMock(),
            ),
            patch("quarry.database.list_documents", return_value=[]),
            patch(
                "quarry.pipeline.ingest_content",
                side_effect=RuntimeError("embedding failed"),
            ),
        ):
            _ingest_background()

        assert not text_file.exists()


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
