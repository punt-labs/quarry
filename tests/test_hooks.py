"""Tests for the hooks dispatcher and handlers."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from quarry.__main__ import app
from quarry._stdlib import HookConfig, load_hook_config, read_hook_stdin
from quarry.hooks import (
    _collection_for_cwd_conn,
    _find_registration,
    _unique_collection_name,
    extract_message_text,
    extract_transcript_text,
    handle_post_web_fetch,
    handle_pre_compact,
    handle_session_start,
)
from quarry.sync_registry import DirectoryRegistration, SyncRegistry

if TYPE_CHECKING:
    import pytest

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
        conn = SyncRegistry(tmp_path / "r.db")
        project = tmp_path / "myproject"
        project.mkdir()
        assert _unique_collection_name(conn, project) == "myproject"
        conn.close()

    def test_disambiguates_with_parent(self, tmp_path: Path) -> None:
        conn = SyncRegistry(tmp_path / "r.db")
        # Register a different directory with the same leaf name.
        other = tmp_path / "other" / "myproject"
        other.mkdir(parents=True)
        conn.register_directory(other, "myproject")

        project = tmp_path / "mine" / "myproject"
        project.mkdir(parents=True)
        name = _unique_collection_name(conn, project)
        assert name == "myproject-mine"
        conn.close()

    def test_falls_back_to_hash_on_double_collision(self, tmp_path: Path) -> None:
        conn = SyncRegistry(tmp_path / "r.db")
        # Occupy both "myproject" and "myproject-mine".
        d1 = tmp_path / "a" / "myproject"
        d1.mkdir(parents=True)
        conn.register_directory(d1, "myproject")

        d2 = tmp_path / "b" / "myproject"
        d2.mkdir(parents=True)
        conn.register_directory(d2, "myproject-mine")

        project = tmp_path / "mine" / "myproject"
        project.mkdir(parents=True)
        name = _unique_collection_name(conn, project)
        assert name.startswith("myproject-")
        assert len(name) == len("myproject-") + 8  # 8-char hash
        conn.close()


class TestCollectionForCwdConn:
    def test_returns_collection_for_exact_match(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        conn = SyncRegistry(tmp_path / "registry.db")
        conn.register_directory(project, "myproject")
        assert _collection_for_cwd_conn(conn, str(project)) == "myproject"
        conn.close()

    def test_returns_collection_for_subdirectory(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        subdir = project / "src" / "lib"
        subdir.mkdir(parents=True)
        conn = SyncRegistry(tmp_path / "registry.db")
        conn.register_directory(project, "myproject")
        assert _collection_for_cwd_conn(conn, str(subdir)) == "myproject"
        conn.close()

    def test_returns_none_for_unregistered_directory(self, tmp_path: Path) -> None:
        conn = SyncRegistry(tmp_path / "registry.db")
        assert _collection_for_cwd_conn(conn, str(tmp_path / "unregistered")) is None
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
    def test_returns_launched_on_success(self, tmp_path: Path) -> None:
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
            assert _sync_in_background() == "launched"
            assert lockfile.exists()
            assert lockfile.read_text() == "99999"

    def test_returns_failed_on_oserror(self, tmp_path: Path) -> None:
        import subprocess as _subprocess

        from quarry.hooks import _sync_in_background

        lockfile = tmp_path / "sync.pid"
        with (
            patch.object(_subprocess, "Popen", side_effect=OSError("No such file")),
            patch("quarry.hooks._is_sync_running", return_value=False),
            patch("quarry.hooks._sync_lockfile", return_value=lockfile),
        ):
            assert _sync_in_background() == "failed"
            assert not lockfile.exists()  # Lock cleaned up on failure

    def test_returns_running_when_already_running(self) -> None:
        from quarry.hooks import _sync_in_background

        with patch("quarry.hooks._is_sync_running", return_value=True):
            assert _sync_in_background() == "running"

    def test_returns_running_when_lock_held(self, tmp_path: Path) -> None:
        """Atomic lock prevents TOCTOU race — second caller gets 'running'."""
        from quarry.hooks import _sync_in_background

        lockfile = tmp_path / "sync.pid"
        lockfile.write_text("12345")  # Pre-existing lock file
        with (
            patch("quarry.hooks._is_sync_running", return_value=False),
            patch("quarry.hooks._sync_lockfile", return_value=lockfile),
        ):
            assert _sync_in_background() == "running"

    def test_pidfile_write_failure_still_returns_launched(self, tmp_path: Path) -> None:
        """If Popen succeeds but PID write fails, sync is running — return launched."""
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
            # Sync launched despite write failure.
            assert _sync_in_background() == "launched"


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
            patch(
                "quarry.hooks._sync_in_background",
                return_value="launched",
            ) as mock_sync,
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
        conn = SyncRegistry(settings.registry_path)
        regs = conn.list_registrations()
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
            patch("quarry.hooks._sync_in_background", return_value="failed"),
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "Background sync failed to launch." in ctx

    def test_context_reflects_sync_already_running(self, tmp_path: Path) -> None:
        project = tmp_path / "runningproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        with (
            patch("quarry.hooks._resolve_settings", return_value=settings),
            patch("quarry.hooks._sync_in_background", return_value="running"),
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "Background sync already running." in ctx

    def test_skips_registration_when_already_registered(self, tmp_path: Path) -> None:
        project = tmp_path / "existing"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        # Pre-register the directory.
        conn = SyncRegistry(settings.registry_path)
        conn.register_directory(project, "custom-name")
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
        conn = SyncRegistry(settings.registry_path)
        conn.register_directory(other, "myproject")
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
        conn = SyncRegistry(settings.registry_path)
        regs = conn.list_registrations()
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
            patch("quarry.hooks._sync_in_background", return_value="launched"),
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert ctx.startswith("Quarry semantic search is active")


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
        with patch("quarry.ingestion.pipeline.ingest_content") as mock_ingest:
            result = handle_post_web_fetch(payload)
        assert result == {}
        mock_ingest.assert_not_called()

    def test_sends_html_content_to_daemon_capture(self) -> None:
        """Fetched HTML goes to the daemon capture path (it extracts + scrubs)."""
        payload: dict[str, object] = {
            "tool_input": {"url": "https://example.com/page?token=secret"},
            "tool_response": json.dumps({"result": "<html>Page content</html>"}),
        }

        with (
            patch("quarry.hooks._capture_via_daemon", return_value=True) as cap,
            patch("quarry.hooks._ingest_url_via_daemon") as ing,
        ):
            result = handle_post_web_fetch(payload)

        assert result == {}
        ing.assert_not_called()
        req = cap.call_args[0][0]
        assert "<html>Page content</html>" in req.content
        assert req.format_hint == "html"
        # userinfo/query must be redacted from the stored document name.
        assert "token=secret" not in req.document_name

    def test_falls_back_to_ingest_url_without_content(self) -> None:
        """No usable content -> the daemon re-fetches via the SSRF-checked route."""
        payload: dict[str, object] = {
            "tool_input": {"url": "https://example.com/page"},
        }

        with (
            patch("quarry.hooks._capture_via_daemon") as cap,
            patch("quarry.hooks._ingest_url_via_daemon", return_value=True) as ing,
        ):
            result = handle_post_web_fetch(payload)

        assert result == {}
        cap.assert_not_called()
        req = ing.call_args[0][0]
        assert req.source == "https://example.com/page"

    def test_passes_cwd_for_server_side_collection(self) -> None:
        """Both paths send cwd so the daemon derives the <repo>-captures target."""
        content_payload: dict[str, object] = {
            "cwd": "/projects/myapp",
            "tool_input": {"url": "https://example.com/docs"},
            "tool_response": json.dumps({"result": "<p>Some docs</p>"}),
        }
        fetch_payload: dict[str, object] = {
            "cwd": "/projects/myapp",
            "tool_input": {"url": "https://example.com/page"},
        }

        with (
            patch("quarry.hooks._capture_via_daemon", return_value=True) as cap,
            patch("quarry.hooks._ingest_url_via_daemon", return_value=True) as ing,
        ):
            handle_post_web_fetch(content_payload)
            handle_post_web_fetch(fetch_payload)

        assert cap.call_args[0][0].cwd == "/projects/myapp"
        assert ing.call_args[0][0].cwd == "/projects/myapp"

    def test_down_daemon_logs_page_not_indexed_not_backfill(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A web fetch writes no durable copy, so a down daemon must not promise
        a backfill that will never run — the log says the page is not indexed."""
        from quarry.client import QuarryConnectionError

        payload: dict[str, object] = {
            "tool_input": {"url": "https://example.com/p"},
            "tool_response": json.dumps({"result": "<html>hi</html>"}),
        }
        with (
            patch(
                "quarry.client.TargetResolver.connect",
                side_effect=QuarryConnectionError("down", "url"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = handle_post_web_fetch(payload)

        assert result == {}
        assert "page not indexed" in caplog.text
        assert "backfill" not in caplog.text


class TestHookImportsNoEngine:
    """The capture hook paths must run with the engine libraries poisoned.

    This is the runtime gate the import-linter cannot provide: the hook's engine
    imports (if any) are lazy, so a static rule sees nothing.  Poison lancedb and
    onnxruntime so *any* import of them (directly or via the pipeline/db) raises,
    then run the pre-compact and web-fetch capture paths — they must complete.
    """

    def test_capture_paths_run_with_engine_poisoned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # None in sys.modules makes ``import <name>`` raise ImportError, which
        # transitively poisons anything (pipeline, db) that pulls the engine in.
        for name in ("lancedb", "onnxruntime", "quarry.ingestion.pipeline"):
            monkeypatch.setitem(sys.modules, name, None)

        import importlib

        from quarry import _hook_entry

        # The entry point itself must import stdlib-only.
        importlib.reload(_hook_entry)

        transcript = _make_transcript(tmp_path, "hello world")
        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch("quarry.hooks._capture_via_daemon", return_value=True) as pre_cap,
        ):
            handle_pre_compact(
                {"transcript_path": str(transcript), "session_id": "abcd1234ef"}
            )
        assert pre_cap.called

        with (
            patch("quarry.hooks._capture_via_daemon", return_value=True) as web_cap,
            patch("quarry.hooks._ingest_url_via_daemon", return_value=True),
        ):
            handle_post_web_fetch(
                {
                    "tool_input": {"url": "https://example.com/p"},
                    "tool_response": json.dumps({"result": "<html>hi</html>"}),
                }
            )
        assert web_cap.called


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
        result = extract_message_text(record)
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
        result = extract_message_text(record)
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
        result = extract_message_text(record)
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
        result = extract_message_text(record)
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
        text = extract_transcript_text(str(transcript))
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
        text = extract_transcript_text(str(transcript))
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
        text = extract_transcript_text(str(transcript))
        assert "[assistant] Done" in text
        assert "Bash" not in text

    def test_returns_empty_for_nonexistent_file(self) -> None:
        assert extract_transcript_text("/nonexistent/path.jsonl") == ""

    def test_returns_empty_for_empty_file(self, tmp_path: Path) -> None:
        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("")
        assert extract_transcript_text(str(transcript)) == ""

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

        text = extract_transcript_text(str(transcript))
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
        text = extract_transcript_text(str(transcript))
        # First message (AAA...) should be dropped, last two kept.
        assert "A" * 50 not in text
        assert msg2_text in text
        assert msg3_text in text

    def test_returns_empty_for_unreadable_file(self, tmp_path: Path) -> None:
        transcript = tmp_path / "binary.jsonl"
        transcript.write_bytes(b"\x80\x81\x82\xff\xfe")
        assert extract_transcript_text(str(transcript)) == ""


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

    def test_sends_capture_request_to_daemon(self, tmp_path: Path) -> None:
        """The transcript text, cwd, and session travel to the daemon as a capture."""
        transcript = _make_transcript(tmp_path, "Important context here")

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch("quarry.hooks._capture_via_daemon", return_value=True) as cap,
        ):
            result = handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        req = cap.call_args[0][0]
        assert "Important context here" in req.content
        assert req.session_id == "abc12345-full-id"
        assert req.format_hint == "markdown"
        assert "systemMessage" in result

    def test_passes_cwd_and_agent_for_server_derivation(self, tmp_path: Path) -> None:
        """The hook sends cwd (daemon derives <repo>-captures) and the agent handle."""
        transcript = _make_transcript(tmp_path, "Working on myapp")

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch("quarry.hooks._read_ethos_agent_handle", return_value="rmh"),
            patch("quarry.hooks._write_capture_file"),
            patch("quarry.hooks._capture_via_daemon", return_value=True) as cap,
        ):
            handle_pre_compact(
                {
                    "cwd": "/projects/myapp",
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        req = cap.call_args[0][0]
        assert req.cwd == "/projects/myapp"
        assert req.agent_handle == "rmh"

    def test_empty_transcript_skips_ingestion(self, tmp_path: Path) -> None:
        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("")

        with patch("quarry.hooks._capture_via_daemon") as cap:
            result = handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc123",
                }
            )

        assert result == {}
        cap.assert_not_called()

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
            patch("quarry.hooks._capture_via_daemon", return_value=True),
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
            patch("quarry.hooks._capture_via_daemon", return_value=True),
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
        """The daemon capture still runs even when archival raises an exception."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Hello"}]}}\n'
        )

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch("quarry.hooks.shutil.copy", side_effect=OSError("disk full")),
            patch("quarry.hooks._capture_via_daemon", return_value=True) as cap,
        ):
            handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        cap.assert_called_once()

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
            patch("quarry.hooks._capture_via_daemon", return_value=True),
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
            patch("quarry.hooks._capture_via_daemon", return_value=True),
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

    def test_system_message_is_collection_generic(self, tmp_path: Path) -> None:
        """The message names no collection or document — the daemon owns them."""
        transcript = _make_transcript(tmp_path, "Confirm capture")

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch("quarry.hooks._capture_via_daemon", return_value=True),
        ):
            result = handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        assert "systemMessage" in result
        msg = str(result["systemMessage"])
        assert msg.startswith("Capturing")
        assert "/find" in msg
        assert "chunks" not in msg
        assert "-captures" not in msg

    def test_daemon_down_returns_backfill_warning(self, tmp_path: Path) -> None:
        """A down daemon leaves the durable archive and nudges backfill-sessions."""
        transcript = _make_transcript(tmp_path, "Will not be indexed now")

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch("quarry.hooks._capture_via_daemon", return_value=False),
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
        assert "backfill-sessions" in msg

    def test_system_message_uses_present_tense(self, tmp_path: Path) -> None:
        """systemMessage says 'Capturing' not 'captured' (async honesty)."""
        transcript = _make_transcript(tmp_path)

        with (
            patch("quarry.hooks.Path.home", return_value=tmp_path / "home"),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.hooks._capture_via_daemon", return_value=True),
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


class TestHookCLI:
    """The CLI dispatcher reads stdin JSON, calls the handler, writes stdout."""

    def test_session_start_no_cwd_returns_empty_json(self) -> None:
        result = runner.invoke(app, ["hooks", "session-start"], input="")
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {}

    def test_post_web_fetch_accepts_json_stdin(self) -> None:
        payload = json.dumps({"tool_input": {"url": "https://example.com"}})
        result = runner.invoke(app, ["hooks", "post-web-fetch"], input=payload)
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {}

    def test_pre_compact_accepts_empty_stdin(self) -> None:
        result = runner.invoke(app, ["hooks", "pre-compact"], input="")
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {}

    def test_hooks_help(self) -> None:
        result = runner.invoke(app, ["hooks", "--help"])
        assert result.exit_code == 0
        assert "session-start" in result.output
        assert "post-web-fetch" in result.output
        assert "pre-compact" in result.output

    def test_invalid_json_is_fail_open(self) -> None:
        result = runner.invoke(app, ["hooks", "session-start"], input="not json{{{")
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {}


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


# ---------------------------------------------------------------------------
# Enable/disable hook routing tests — T15 through T20
# ---------------------------------------------------------------------------


class TestT15SessionStartChildUsesParentCollection:
    """T15: session-start on child directory uses parent collection."""

    def test_child_directory_uses_parent_collection(self, tmp_path: Path) -> None:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        # Register the parent directory.
        conn = SyncRegistry(settings.registry_path)
        conn.register_directory(parent, "proj")
        conn.close()

        with (
            patch("quarry.hooks._resolve_settings", return_value=settings),
            patch("quarry.hooks._sync_in_background", return_value="launched"),
        ):
            result = handle_session_start({"cwd": str(child)})

        # No ValueError raised -- the child-directory crash is fixed.
        assert "hookSpecificOutput" in result
        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "proj" in ctx

        # Verify no new registration was created.
        conn = SyncRegistry(settings.registry_path)
        regs = conn.list_registrations()
        conn.close()
        assert len(regs) == 1
        assert regs[0].collection == "proj"


class TestT16SessionStartAutoRegisters:
    """T16: session-start on unregistered directory auto-registers."""

    def test_auto_registers_unregistered_directory(self, tmp_path: Path) -> None:
        project = tmp_path / "newproject"
        project.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        # Create empty registry.
        conn = SyncRegistry(settings.registry_path)
        conn.close()

        with (
            patch("quarry.hooks._resolve_settings", return_value=settings),
            patch("quarry.hooks._sync_in_background", return_value="launched"),
        ):
            result = handle_session_start({"cwd": str(project)})

        assert "hookSpecificOutput" in result
        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)

        # Verify registration was created.
        conn = SyncRegistry(settings.registry_path)
        regs = conn.list_registrations()
        conn.close()
        assert len(regs) == 1
        assert regs[0].directory == str(project)


class TestT16bSessionStartParentOfChildrenSkipsAutoRegister:
    """T16b: session-start on parent of existing children skips auto-register."""

    def test_skips_auto_register_with_children(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        parent = tmp_path / "parent"
        parent.mkdir()
        child_a = parent / "child-a"
        child_a.mkdir()
        child_b = parent / "child-b"
        child_b.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        # Register children only.
        conn = SyncRegistry(settings.registry_path)
        conn.register_directory(child_a, "child-a")
        conn.register_directory(child_b, "child-b")
        conn.close()

        with (
            patch("quarry.hooks._resolve_settings", return_value=settings),
            patch("quarry.hooks._sync_in_background", return_value="launched"),
            caplog.at_level(logging.WARNING, logger="quarry.hooks"),
        ):
            result = handle_session_start({"cwd": str(parent)})

        # No new registration for /parent.
        conn = SyncRegistry(settings.registry_path)
        regs = conn.list_registrations()
        conn.close()
        assert len(regs) == 2
        collections = {r.collection for r in regs}
        assert "child-a" in collections
        assert "child-b" in collections

        # Warning logged.
        assert any(
            "existing child registrations found" in rec.message
            for rec in caplog.records
        )
        assert any(
            "skipping auto-register to prevent subsumption" in rec.message
            for rec in caplog.records
        )

        # Context indicates subsumption warning.
        assert "hookSpecificOutput" in result
        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output.get("additionalContext", ""))
        assert "child registrations exist" in ctx


class TestPreCompactCaptureRedaction:
    """The PreCompact producer writes a PII-clean capture file (bug class 3)."""

    def test_capture_file_has_zero_pii(self, tmp_path: Path) -> None:
        from quarry.artifacts import SessionArtifacts
        from quarry.hooks import _write_capture_file

        artifacts = SessionArtifacts(
            commit_shas=(),
            pr_numbers=(),
            branch_names=(),
            bead_ids=(),
        )
        text = "worked in /Users/jfreeman/repo and pinged jmf@pobox.com"
        _write_capture_file(
            project_dir=tmp_path,
            session_id="abcd1234ef",
            timestamp="2026-07-11T00:00:00Z",
            artifacts=artifacts,
            text=text,
        )

        capture = (
            tmp_path / ".punt-labs" / "quarry" / "captures" / "session-abcd1234.md"
        )
        content = capture.read_text(encoding="utf-8")
        assert "/Users/" not in content
        assert "@" not in content
        assert "~/repo" in content
