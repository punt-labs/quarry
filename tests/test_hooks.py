"""Tests for the hooks dispatcher and handlers."""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from quarry.__main__ import app
from quarry._hook_trace import HookPayload
from quarry._stdlib import HookConfig, load_hook_config, read_hook_stdin
from quarry.enabled_marker import EnabledMarker
from quarry.hooks import (
    handle_post_web_fetch,
    handle_pre_compact,
    handle_session_start,
)
from quarry.sync_registry import SyncRegistry


def _opt_in(project: Path) -> Path:
    """Write the ``.punt-labs/quarry/enabled`` marker so session-start takes Path A."""
    EnabledMarker(project).write()
    return project


runner = CliRunner()


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

    def test_session_end_and_subagent_stop_default_to_compaction(
        self, tmp_path: Path
    ) -> None:
        """An operator disabling compaction expects both transcript captures off."""
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  compaction: false\n---\n"
        )
        config = load_hook_config(str(tmp_path))
        assert config.compaction is False
        assert config.session_end is False
        assert config.subagent_stop is False

    def test_session_end_explicit_override_wins_over_compaction(
        self, tmp_path: Path
    ) -> None:
        """An explicit session_end key overrides the compaction-derived default."""
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nauto_capture:\n  compaction: false\n  session_end: true\n---\n"
        )
        config = load_hook_config(str(tmp_path))
        assert config.compaction is False
        assert config.session_end is True
        assert config.subagent_stop is False


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------


class _ReachableDaemonEmptyCatalog:
    """Mixin: model a reachable daemon with an empty chunk catalog for its tests.

    The auto-register-fresh-name path needs a reachable daemon for the
    chunk-collection avoid-set.  Without this, the (correct) fail-closed defer
    path runs under CI — where no daemon is up — so an auto-register assertion
    like ``len(regs) == 1`` sees 0.  Modeling an up daemon with an empty catalog
    lets the picker proceed hermetically, independent of any live quarryd.
    """

    @pytest.fixture(autouse=True)
    def _daemon_up_empty_catalog(self) -> Iterator[None]:
        with patch(
            "quarry.hooks._SessionStartContext._daemon_chunk_collections",
            return_value=frozenset(),
        ):
            yield


class TestHandleSessionStart(_ReachableDaemonEmptyCatalog):
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
        _opt_in(project)

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch(
                "quarry.hooks.SyncLock.launch_background_sync",
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
        _opt_in(project)

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch(
                "quarry.hooks.SyncLock.launch_background_sync", return_value="failed"
            ),
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "Background sync failed to launch." in ctx

    def test_context_reflects_sync_already_running(self, tmp_path: Path) -> None:
        project = tmp_path / "runningproject"
        project.mkdir()
        _opt_in(project)

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch(
                "quarry.hooks.SyncLock.launch_background_sync", return_value="running"
            ),
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "Background sync already running." in ctx

    def test_skips_registration_when_already_registered(self, tmp_path: Path) -> None:
        project = tmp_path / "existing"
        project.mkdir()
        _opt_in(project)

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        # Pre-register the directory.
        conn = SyncRegistry(settings.registry_path)
        conn.register_directory(project, "custom-name")
        conn.close()

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch("quarry.hooks.SyncLock.launch_background_sync") as mock_sync,
        ):
            result = handle_session_start({"cwd": str(project)})

        mock_sync.assert_called_once()

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "custom-name" in ctx

    def test_returns_additional_context_with_slash_commands(
        self, tmp_path: Path
    ) -> None:
        """Active footer surfaces slash commands + researcher agent."""
        project = tmp_path / "repo"
        project.mkdir()
        _opt_in(project)

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch("quarry.hooks.SyncLock.launch_background_sync"),
            patch(
                "quarry.hooks._SessionStartContext._session_coverage",
                return_value={
                    "documents_indexed": 0,
                    "transcripts_captured": 0,
                    "memories_saved": 0,
                },
            ),
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "/find" in ctx
        assert "researcher agent" in ctx

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
        _opt_in(project)

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch("quarry.hooks.SyncLock.launch_background_sync"),
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
        """Active-mode context leads with the identity line plus R1/R2/R3."""
        project = tmp_path / "hintproject"
        project.mkdir()
        _opt_in(project)

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch(
                "quarry.hooks.SyncLock.launch_background_sync", return_value="launched"
            ),
            patch(
                "quarry.hooks._SessionStartContext._session_coverage",
                return_value={
                    "documents_indexed": 0,
                    "transcripts_captured": 0,
                    "memories_saved": 0,
                },
            ),
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert ctx.startswith("Quarry semantic search is active")
        assert (
            "Use find before WebSearch or WebFetch for research, or before "
            "answering a why/how/what-did-we-decide question." in ctx
        )
        assert (
            "Prefer grep for symbol and value lookups; prefer find for meaning." in ctx
        )
        assert (
            "Use remember when you learn something durable — a decision, a gotcha, "
            "a non-obvious fact, a procedure — so it survives context compaction."
            in ctx
        )


class TestSessionStartTriggerRules(_ReachableDaemonEmptyCatalog):
    """SessionStart context carries the three canonical R1/R2/R3 sentences.

    Each surface — reachable-coverage, unreachable-coverage, subsumption,
    daemon-unreachable-auto-register — must emit the sentences verbatim so an
    agent reads the same rules regardless of which branch fired (design R2b).
    """

    _R1 = (
        "Use find before WebSearch or WebFetch for research, or before "
        "answering a why/how/what-did-we-decide question."
    )
    _R2 = "Prefer grep for symbol and value lookups; prefer find for meaning."
    _R3 = (
        "Use remember when you learn something durable — a decision, a gotcha, "
        "a non-obvious fact, a procedure — so it survives context compaction."
    )

    @staticmethod
    def _settings(tmp_path: Path) -> MagicMock:
        s = MagicMock()
        s.registry_path = tmp_path / "registry.db"
        s.lancedb_path = tmp_path / "lancedb"
        return s

    def _assert_trailer(self, ctx: str) -> None:
        assert self._R1 in ctx
        assert self._R2 in ctx
        assert self._R3 in ctx

    def test_active_reachable_coverage_line_and_trailer(self, tmp_path: Path) -> None:
        project = tmp_path / "reachable"
        project.mkdir()
        _opt_in(project)
        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=self._settings(tmp_path),
            ),
            patch(
                "quarry.hooks.SyncLock.launch_background_sync", return_value="launched"
            ),
            patch(
                "quarry.hooks._SessionStartContext._session_coverage",
                return_value={
                    "documents_indexed": 42,
                    "transcripts_captured": 7,
                    "memories_saved": 3,
                },
            ),
        ):
            result = handle_session_start({"cwd": str(project)})
        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "42 documents indexed" in ctx
        assert "7 transcripts captured" in ctx
        assert "3 memories saved" in ctx
        assert "reachable-captures" in ctx
        self._assert_trailer(ctx)

    def test_active_unreachable_coverage_falls_back_and_keeps_trailer(
        self, tmp_path: Path
    ) -> None:
        """A coverage-query failure does not withhold the trigger rules."""
        project = tmp_path / "unreachcov"
        project.mkdir()
        _opt_in(project)
        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=self._settings(tmp_path),
            ),
            patch(
                "quarry.hooks.SyncLock.launch_background_sync", return_value="launched"
            ),
            patch(
                "quarry.hooks._SessionStartContext._session_coverage", return_value=None
            ),
        ):
            result = handle_session_start({"cwd": str(project)})
        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "unavailable" in ctx
        self._assert_trailer(ctx)

    def test_subsumption_branch_carries_trailer(self, tmp_path: Path) -> None:
        """Child registrations under this dir refuse auto-register + emit rules."""
        parent = tmp_path / "parent"
        parent.mkdir()
        _opt_in(parent)
        child = parent / "child"
        child.mkdir()
        settings = self._settings(tmp_path)
        conn = SyncRegistry(settings.registry_path)
        conn.register_directory(child, "child")
        conn.close()
        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch("quarry.hooks.SyncLock.launch_background_sync") as sync,
        ):
            result = handle_session_start({"cwd": str(parent)})
        sync.assert_not_called()
        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "child registrations exist" in ctx
        self._assert_trailer(ctx)


class TestSessionStartDaemonUnreachableCarriesTrailer:
    """R2b: a daemon-unreachable auto-register defer still emits R1/R2/R3.

    The design body proposed withholding the trailer here on the reasoning
    that ``find``/``remember`` would fail at the client boundary. R2b reverses
    that: an agent reading the message can act on the diagnosis (restart
    quarryd) and then apply the rules once the tools return.
    """

    _R1 = (
        "Use find before WebSearch or WebFetch for research, or before "
        "answering a why/how/what-did-we-decide question."
    )
    _R2 = "Prefer grep for symbol and value lookups; prefer find for meaning."
    _R3 = (
        "Use remember when you learn something durable — a decision, a gotcha, "
        "a non-obvious fact, a procedure — so it survives context compaction."
    )

    def test_unreachable_defer_carries_r1_r2_r3_and_restart_hint(
        self, tmp_path: Path
    ) -> None:
        from quarry.client import QuarryConnectionError

        project = tmp_path / "solo"
        project.mkdir()
        _opt_in(project)
        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"
        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch("quarry.hooks.SyncLock.launch_background_sync"),
            patch(
                "quarry.client.TargetResolver.connect",
                side_effect=QuarryConnectionError("down", "url"),
            ),
        ):
            result = handle_session_start({"cwd": str(project)})
        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "unreachable" in ctx
        assert "systemctl --user restart quarry" in ctx
        assert self._R1 in ctx
        assert self._R2 in ctx
        assert self._R3 in ctx


class TestSessionStartReadopt:
    """Session-start re-adopts a cwd-owned keep-data archive end-to-end."""

    def test_reenable_reuses_archived_collection_name(self, tmp_path: Path) -> None:
        # backend was enabled then keep-data-disabled: its chunks are archived
        # under the directory it was registered from. Re-opening a session in that
        # same directory must re-adopt the "backend" collection (reusing its kept
        # index), NOT mint a fresh disambiguated name.
        project = tmp_path / "backend"
        project.mkdir()
        _opt_in(project)

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        conn = SyncRegistry(settings.registry_path)
        conn.register_directory(project, "backend")
        conn.deregister_directory("backend", keep_data=True)  # archive it
        conn.close()

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch("quarry.hooks.SyncLock.launch_background_sync"),
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        assert "backend" in str(output["additionalContext"])

        conn = SyncRegistry(settings.registry_path)
        regs = conn.list_registrations()
        retained = conn.markers.list_retained()
        conn.close()
        assert [r.collection for r in regs] == ["backend"]  # re-adopted, not fresh
        assert retained == []  # marker cleared by the re-adopt


class TestSessionStartFailsClosedWhenDaemonUnreachable:
    """A fresh auto-registration needs the daemon's chunk set to be merge-safe.

    Without it, the picker would run against an empty avoid-set and could hand a
    different directory a name that already holds another project's chunks; the
    local ``register_directory`` then clears the orphan sweep's pending mark,
    arming a cross-project merge the moment the daemon returns.  So a cwd that
    owns no archive and cannot reach the daemon must DEFER, not register.  A cwd
    that owns an archive re-adopts from LOCAL markers and is unaffected.
    """

    @staticmethod
    def _settings(tmp_path: Path) -> MagicMock:
        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"
        return settings

    def test_unreachable_daemon_no_archive_defers_and_nudges(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # No coverage, no descendants, no archive, daemon DOWN -> fail closed:
        # no registry row, no background sync, a nudge to start quarryd.  The old
        # fail-open code registered "solo" and launched sync (the latent merge).
        from quarry.client import QuarryConnectionError

        project = tmp_path / "solo"
        project.mkdir()
        _opt_in(project)
        settings = self._settings(tmp_path)

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch("quarry.hooks.SyncLock.launch_background_sync") as mock_sync,
            patch(
                "quarry.client.TargetResolver.connect",
                side_effect=QuarryConnectionError("down", "url"),
            ),
            caplog.at_level(logging.WARNING, logger="quarry.hooks"),
        ):
            result = handle_session_start({"cwd": str(project)})

        conn = SyncRegistry(settings.registry_path)
        regs = conn.list_registrations()
        conn.close()
        assert regs == []  # fail closed: no armed registration
        mock_sync.assert_not_called()  # no sync into an unverifiable collection

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "unreachable" in ctx
        assert "deferred" in ctx
        assert any("deferring auto-registration" in r.message for r in caplog.records)

    def test_unreachable_daemon_with_archive_still_readopts(
        self, tmp_path: Path
    ) -> None:
        # The SAME directory that owns a keep-data archive re-adopts it by name
        # from LOCAL retained markers, BEFORE the picker — so a down daemon never
        # blocks the re-adopt of its own kept chunks.
        from quarry.client import QuarryConnectionError

        project = tmp_path / "backend"
        project.mkdir()
        _opt_in(project)
        settings = self._settings(tmp_path)

        conn = SyncRegistry(settings.registry_path)
        conn.register_directory(project, "backend")
        conn.deregister_directory("backend", keep_data=True)  # archive it
        conn.close()

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch("quarry.hooks.SyncLock.launch_background_sync") as mock_sync,
            patch(
                "quarry.client.TargetResolver.connect",
                side_effect=QuarryConnectionError("down", "url"),
            ),
        ):
            result = handle_session_start({"cwd": str(project)})

        conn = SyncRegistry(settings.registry_path)
        regs = conn.list_registrations()
        conn.close()
        assert [r.collection for r in regs] == ["backend"]  # re-adopted despite down
        mock_sync.assert_called_once()
        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        assert "backend" in str(output["additionalContext"])

    def test_reachable_daemon_empty_catalog_registers_fresh(
        self, tmp_path: Path
    ) -> None:
        # Daemon UP, genuinely empty chunk catalog: an empty set is a VERIFIED
        # answer, not an unreachable one, so the picker proceeds and registers.
        project = tmp_path / "fresh"
        project.mkdir()
        _opt_in(project)
        settings = self._settings(tmp_path)

        client = MagicMock()
        client.list_registrations.return_value.chunk_collections = []

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch("quarry.hooks.SyncLock.launch_background_sync") as mock_sync,
            patch("quarry.client.TargetResolver.connect", return_value=client),
        ):
            handle_session_start({"cwd": str(project)})

        conn = SyncRegistry(settings.registry_path)
        regs = conn.list_registrations()
        conn.close()
        assert [r.collection for r in regs] == ["fresh"]  # registered normally
        mock_sync.assert_called_once()


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
        with patch("quarry.ingestion.web_ingest.ingest_content") as mock_ingest:
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
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as cap,
            patch("quarry.daemon_capture.DaemonCaptureSender.send_ingest_url") as ing,
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
            patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap,
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_ingest_url",
                return_value=True,
            ) as ing,
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
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as cap,
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_ingest_url",
                return_value=True,
            ) as ing,
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

    def test_http_error_logs_status_not_unreachable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-2xx means the daemon is UP but rejected the request (auth,
        server, validation).  It must be logged with its status, never collapsed
        into 'unreachable' — that would send an operator chasing a phantom down
        daemon when the real cause is a 401/500."""
        from quarry.api import CapturesLookupResponse
        from quarry.client import HttpError

        client = MagicMock()
        client.capture.side_effect = HttpError("Unauthorized", 401, "")
        client.captures_lookup.return_value = CapturesLookupResponse(matched=False)
        payload: dict[str, object] = {
            "tool_input": {"url": "https://example.com/p"},
            "tool_response": json.dumps({"result": "<html>hi</html>"}),
        }
        with (
            patch("quarry.client.TargetResolver.connect", return_value=client),
            caplog.at_level(logging.WARNING),
        ):
            result = handle_post_web_fetch(payload)

        assert result == {}
        assert "daemon rejected request" in caplog.text
        assert "401" in caplog.text
        # Not misclassified as a connection failure:
        assert "page not indexed" not in caplog.text
        assert "unreachable" not in caplog.text

    def test_config_error_logs_misconfigured_not_unreachable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A local misconfig (e.g. QUARRY_URL at a refused cleartext remote) is a
        configuration error, not a down daemon — it must be logged as such, never
        collapsed into 'unreachable' or the generic 'malformed response'."""
        from quarry.client import ClientConfigError

        payload: dict[str, object] = {
            "tool_input": {"url": "https://example.com/p"},
            "tool_response": json.dumps({"result": "<html>hi</html>"}),
        }
        with (
            patch(
                "quarry.client.TargetResolver.connect",
                side_effect=ClientConfigError("QUARRY_URL is cleartext http"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = handle_post_web_fetch(payload)

        assert result == {}
        assert "misconfigured" in caplog.text
        assert "cleartext" in caplog.text
        # Not misclassified as down or malformed:
        assert "page not indexed" not in caplog.text
        assert "unreachable" not in caplog.text
        assert "malformed response" not in caplog.text

    def test_capture_success_emits_g6_breadcrumb(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """G6: the happy path leaves an INFO ``-> capture`` breadcrumb.

        The previous round only traced the two early-exit skips; the busiest
        exit — a successful send_capture — returned silently, defeating the
        breadcrumb's purpose.  The line must fire when the daemon accepts the
        capture so an operator can prove the hook ran end to end.
        """
        payload: dict[str, object] = {
            "tool_input": {"url": "https://example.com/page"},
            "tool_response": json.dumps({"result": "<html>Page content</html>"}),
        }
        with (
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ),
            caplog.at_level(logging.INFO, logger="quarry.hooks"),
        ):
            handle_post_web_fetch(payload)

        breadcrumbs = [
            r
            for r in caplog.records
            if r.name == "quarry.hooks" and "post-web-fetch" in r.message
        ]
        assert breadcrumbs, "no post-web-fetch breadcrumb was emitted"
        assert any("-> capture" in r.message for r in breadcrumbs)

    def test_ingest_url_fallback_emits_g6_breadcrumb(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """G6: the fallback re-fetch branch also leaves a breadcrumb.

        Parity with :meth:`test_capture_success_emits_g6_breadcrumb`: the
        no-content-in-payload path must not go dark either.
        """
        payload: dict[str, object] = {
            "tool_input": {"url": "https://example.com/page"},
        }
        with (
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_ingest_url",
                return_value=True,
            ),
            caplog.at_level(logging.INFO, logger="quarry.hooks"),
        ):
            handle_post_web_fetch(payload)

        assert any(
            "post-web-fetch" in r.message and "-> capture" in r.message
            for r in caplog.records
        )


class TestWebFetchLoopCloser:
    """The captures-lookup nudge: matched/no-match/unreachable, fail-open."""

    def test_matched_true_returns_additional_context(self) -> None:
        from quarry.api import CapturesLookupResponse

        client = MagicMock()
        client.captures_lookup.return_value = CapturesLookupResponse(
            matched=True, document_name="https://example.com/docs/guide"
        )
        payload: dict[str, object] = {
            "tool_input": {"url": "https://example.com/docs/guide"},
        }
        with patch("quarry.client.TargetResolver.connect", return_value=client):
            result = handle_post_web_fetch(payload)

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        assert output["hookEventName"] == "PostToolUse"
        context = str(output["additionalContext"])
        assert "https://example.com/docs/guide" in context
        assert "'guide'" in context
        assert "https://example.com/docs/guide" in context.split("Indexed as ")[1]

    def test_matched_true_without_document_name_still_nudges(self) -> None:
        from quarry.api import CapturesLookupResponse

        client = MagicMock()
        client.captures_lookup.return_value = CapturesLookupResponse(matched=True)
        payload: dict[str, object] = {"tool_input": {"url": "https://example.com/x"}}
        with patch("quarry.client.TargetResolver.connect", return_value=client):
            result = handle_post_web_fetch(payload)

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        assert "Indexed as" not in str(output["additionalContext"])

    def test_no_match_returns_empty_dict(self) -> None:
        from quarry.api import CapturesLookupResponse

        client = MagicMock()
        client.captures_lookup.return_value = CapturesLookupResponse(matched=False)
        payload: dict[str, object] = {"tool_input": {"url": "https://example.com/x"}}
        with patch("quarry.client.TargetResolver.connect", return_value=client):
            result = handle_post_web_fetch(payload)

        assert result == {}

    def test_lookup_raising_connection_error_fails_open(self) -> None:
        from quarry.client import QuarryConnectionError

        payload: dict[str, object] = {"tool_input": {"url": "https://example.com/x"}}
        with patch(
            "quarry.client.TargetResolver.connect",
            side_effect=QuarryConnectionError("down", "url"),
        ):
            result = handle_post_web_fetch(payload)

        assert result == {}

    def test_lookup_raising_config_error_fails_open(self) -> None:
        from quarry.client import ClientConfigError

        payload: dict[str, object] = {"tool_input": {"url": "https://example.com/x"}}
        with patch(
            "quarry.client.TargetResolver.connect",
            side_effect=ClientConfigError("bad config"),
        ):
            result = handle_post_web_fetch(payload)

        assert result == {}

    def test_matched_context_redacts_query_fragment_and_userinfo(self) -> None:
        """The URL echoed into additionalContext must never carry secrets.

        additionalContext lands in the session transcript, so a query-string
        token like ``?api_key=secret`` must not survive into the nudge text.
        """
        from quarry.api import CapturesLookupResponse

        client = MagicMock()
        client.captures_lookup.return_value = CapturesLookupResponse(matched=True)
        raw_url = "https://user:pass@example.com/reset?api_key=secret123#frag"
        payload: dict[str, object] = {"tool_input": {"url": raw_url}}
        with patch("quarry.client.TargetResolver.connect", return_value=client):
            result = handle_post_web_fetch(payload)

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        context = str(output["additionalContext"])
        assert "secret123" not in context
        assert "user:pass" not in context
        assert "?" not in context
        assert "#" not in context
        assert "@" not in context
        assert "https://example.com/reset" in context

    def test_suggested_query_falls_back_to_host_for_bare_path(self) -> None:
        from quarry.api import CapturesLookupResponse

        client = MagicMock()
        client.captures_lookup.return_value = CapturesLookupResponse(matched=True)
        payload: dict[str, object] = {"tool_input": {"url": "https://example.com/"}}
        with patch("quarry.client.TargetResolver.connect", return_value=client):
            result = handle_post_web_fetch(payload)

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        assert "'example.com'" in str(output["additionalContext"])


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
            patch(
                "quarry.session_transcript.Path.home", return_value=tmp_path / "home"
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as pre_cap,
        ):
            handle_pre_compact(
                {"transcript_path": str(transcript), "session_id": "abcd1234ef"}
            )
        assert pre_cap.called

        with (
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as web_cap,
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_ingest_url",
                return_value=True,
            ),
        ):
            handle_post_web_fetch(
                {
                    "tool_input": {"url": "https://example.com/p"},
                    "tool_response": json.dumps({"result": "<html>hi</html>"}),
                }
            )
        assert web_cap.called


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

    def test_invalid_transcript_path_skips_not_crashes(self) -> None:
        """An OS-invalid transcript_path (embedded NUL) must no-op per the skip
        contract, not crash the PreCompact hook — the path is untrusted input."""
        payload: dict[str, object] = {
            "transcript_path": "/bad\x00path.jsonl",
            "session_id": "abc123",
        }
        result = handle_pre_compact(payload)
        assert result == {}

    def test_non_string_session_id_skips_cleanly(self, tmp_path: Path) -> None:
        """A non-string session_id (None) is MISSING, not the literal "None".

        Coercing with ``str()`` forged a truthy ``"None"`` that bypassed the
        missing-field guard, producing a bogus ``session-None`` capture; the
        hook must instead skip and never reach the daemon.
        """
        transcript = _make_transcript(tmp_path, "Hello")
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
            result = handle_pre_compact(
                {"transcript_path": str(transcript), "session_id": None}
            )
        assert result == {}
        cap.assert_not_called()

    def test_non_string_transcript_path_skips_cleanly(self, tmp_path: Path) -> None:
        """A non-string transcript_path (None) is MISSING — no phantom-path resolve."""
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
            result = handle_pre_compact(
                {"transcript_path": None, "session_id": "abc123"}
            )
        assert result == {}
        cap.assert_not_called()

    def test_sends_capture_request_to_daemon(self, tmp_path: Path) -> None:
        """The transcript text, cwd, and session travel to the daemon as a capture."""
        transcript = _make_transcript(tmp_path, "Important context here")

        with (
            patch(
                "quarry.session_transcript.Path.home", return_value=tmp_path / "home"
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as cap,
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
            patch(
                "quarry.session_transcript.Path.home", return_value=tmp_path / "home"
            ),
            patch(
                "quarry.ethos_handle.EthosConfig.agent_handle_at", return_value="rmh"
            ),
            patch("quarry.capture.CaptureWriter.write"),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as cap,
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

        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
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
            patch(
                "quarry.session_transcript.Path.home", return_value=tmp_path / "home"
            ),
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ),
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
            patch(
                "quarry.session_transcript.Path.home", return_value=tmp_path / "home"
            ),
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ),
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
            patch(
                "quarry.session_transcript.Path.home", return_value=tmp_path / "home"
            ),
            patch(
                "quarry.transcript_reader.shutil.copy",
                side_effect=OSError("disk full"),
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as cap,
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
            patch(
                "quarry.session_transcript.Path.home", return_value=tmp_path / "home"
            ),
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ),
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
            patch(
                "quarry.session_transcript.Path.home", return_value=tmp_path / "home"
            ),
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ),
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
            patch(
                "quarry.session_transcript.Path.home", return_value=tmp_path / "home"
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
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
        assert msg.startswith("Capturing")
        assert "/find" in msg
        assert "chunks" not in msg
        assert "-captures" not in msg

    def test_daemon_down_returns_backfill_warning(self, tmp_path: Path) -> None:
        """A down daemon leaves the durable archive and nudges backfill-sessions."""
        transcript = _make_transcript(tmp_path, "Will not be indexed now")

        with (
            patch(
                "quarry.session_transcript.Path.home", return_value=tmp_path / "home"
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=False,
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
        assert "backfill-sessions" in msg

    def test_system_message_uses_present_tense(self, tmp_path: Path) -> None:
        """systemMessage says 'Capturing' not 'captured' (async honesty)."""
        transcript = _make_transcript(tmp_path)

        with (
            patch(
                "quarry.session_transcript.Path.home", return_value=tmp_path / "home"
            ),
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ),
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


class TestHookEntryDispatch:
    """``quarry-hook <event>`` reads stdin JSON, calls the handler, writes stdout.

    The hidden ``quarry hooks`` typer sub-app is gone: ``quarry._hook_entry``
    is the one dispatcher, and it must stay fail-open (exit 0, ``{}``) on
    every malformed input so Claude Code is never blocked.
    """

    @staticmethod
    def _run(
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        event: str,
        stdin: str,
    ) -> str:
        from quarry._hook_entry import main

        monkeypatch.setattr(sys, "argv", ["quarry-hook", event])
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        main()
        return capsys.readouterr().out

    def test_session_start_no_cwd_returns_empty_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = self._run(monkeypatch, capsys, "session-start", "")
        assert json.loads(out) == {}

    def test_post_web_fetch_accepts_json_stdin(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        payload = json.dumps({"tool_input": {"url": "https://example.com"}})
        out = self._run(monkeypatch, capsys, "post-web-fetch", payload)
        assert json.loads(out) == {}

    def test_pre_compact_accepts_empty_stdin(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = self._run(monkeypatch, capsys, "pre-compact", "")
        assert json.loads(out) == {}

    def test_invalid_json_is_fail_open(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = self._run(monkeypatch, capsys, "session-start", "not json{{{")
        assert json.loads(out) == {}

    def test_unknown_event_exits_with_usage(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit, match="Unknown hook event: nope"):
            self._run(monkeypatch, capsys, "nope", "")

    def test_cli_no_longer_carries_a_hooks_sub_app(self) -> None:
        result = runner.invoke(app, ["hooks", "--help"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Wiring tests — hooks.json references scripts that exist and are executable
# ---------------------------------------------------------------------------

# The shippable plugin surface lives under ``plugin/``, so the plugin root that
# ``${CLAUDE_PLUGIN_ROOT}`` names at runtime is ``plugin/``, not the repo root.
_HOOKS_DIR = Path(__file__).resolve().parent.parent / "plugin" / "hooks"


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
        _opt_in(parent)
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
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch(
                "quarry.hooks.SyncLock.launch_background_sync", return_value="launched"
            ),
        ):
            result = handle_session_start({"cwd": str(child)})

        # No ValueError raised -- the child-directory crash is fixed.
        assert "hookSpecificOutput" in result
        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        # Path A active flow, not Path C drift-surfacing: the marker lives at
        # the parent root, not the child cwd, and must still be honored.
        assert ctx.startswith("Quarry semantic search is active for this project.")
        assert 'Collection: "proj"' in ctx

        # Verify no new registration was created.
        conn = SyncRegistry(settings.registry_path)
        regs = conn.list_registrations()
        conn.close()
        assert len(regs) == 1
        assert regs[0].collection == "proj"


class TestT16SessionStartAutoRegisters(_ReachableDaemonEmptyCatalog):
    """T16: session-start on unregistered directory auto-registers."""

    def test_auto_registers_unregistered_directory(self, tmp_path: Path) -> None:
        project = tmp_path / "newproject"
        project.mkdir()
        _opt_in(project)

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        # Create empty registry.
        conn = SyncRegistry(settings.registry_path)
        conn.close()

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch(
                "quarry.hooks.SyncLock.launch_background_sync", return_value="launched"
            ),
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
        _opt_in(parent)
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
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch(
                "quarry.hooks.SyncLock.launch_background_sync", return_value="launched"
            ),
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


class TestSessionStartMarkerGate(_ReachableDaemonEmptyCatalog):
    """The § 2.11 marker gate keys SessionStart on ``.punt-labs/quarry/enabled``.

    Three paths — Path A active flow when the marker is present; Path B
    nudge when neither marker nor covering registration exists; Path C
    drift-surface when a covering registration exists without the marker.
    Path B and Path C never mutate the registry and never launch a sync.
    """

    def _settings(self, tmp_path: Path) -> MagicMock:
        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"
        return settings

    def test_path_a_marker_and_registration_runs_active_flow(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _opt_in(project)
        settings = self._settings(tmp_path)
        conn = SyncRegistry(settings.registry_path)
        conn.register_directory(project, "proj")
        conn.close()

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch(
                "quarry.hooks.SyncLock.launch_background_sync", return_value="launched"
            ) as sync,
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert ctx.startswith("Quarry semantic search is active for this project.")

        sync.assert_called_once()

        conn = SyncRegistry(settings.registry_path)
        regs = conn.list_registrations()
        conn.close()
        assert len(regs) == 1  # covering row reused, not duplicated
        assert regs[0].collection == "proj"

    def test_path_b_no_marker_no_registration_nudges_enable(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "unopted"
        project.mkdir()
        settings = self._settings(tmp_path)

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch("quarry.hooks.SyncLock.launch_background_sync") as sync,
            patch(
                "quarry.hooks._SessionStartContext._daemon_chunk_collections"
            ) as daemon,
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert "not enabled" in ctx
        assert f"quarry enable {project}" in ctx
        assert "quarry deregister" not in ctx

        conn = SyncRegistry(settings.registry_path)
        regs = conn.list_registrations()
        conn.close()
        assert regs == []  # Path B never touches the registry
        sync.assert_not_called()
        daemon.assert_not_called()  # Path B never consults the daemon

    def test_path_c_no_marker_registration_exists_surfaces_drift(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        project = tmp_path / "drifted"
        project.mkdir()
        settings = self._settings(tmp_path)
        conn = SyncRegistry(settings.registry_path)
        conn.register_directory(project, "drifted")
        conn.close()

        with (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch("quarry.hooks.SyncLock.launch_background_sync") as sync,
            caplog.at_level(logging.WARNING, logger="quarry.hooks"),
        ):
            result = handle_session_start({"cwd": str(project)})

        output = result["hookSpecificOutput"]
        assert isinstance(output, dict)
        ctx = str(output["additionalContext"])
        assert f"quarry enable {project}" in ctx
        assert "quarry deregister drifted" in ctx

        conn = SyncRegistry(settings.registry_path)
        regs = conn.list_registrations()
        conn.close()
        assert len(regs) == 1  # neither auto-register nor auto-deregister
        assert regs[0].collection == "drifted"

        sync.assert_not_called()
        assert any("no opt-in marker" in rec.message for rec in caplog.records)


class TestPreCompactCaptureRedaction:
    """The PreCompact producer writes a PII-clean capture file (bug class 3)."""

    def test_capture_file_has_zero_pii(self, tmp_path: Path) -> None:
        from quarry.artifacts import SessionArtifacts
        from quarry.capture import CaptureRequest, CaptureWriter

        artifacts = SessionArtifacts(
            commit_shas=(),
            pr_numbers=(),
            branch_names=(),
            bead_ids=(),
        )
        text = "worked in /Users/jdoe/repo and pinged jdoe@example.com"
        CaptureWriter().write(
            CaptureRequest(
                project_dir=tmp_path,
                session_id="abcd1234ef",
                timestamp="2026-07-11T00:00:00Z",
                artifacts=artifacts,
                text=text,
                label="pre-compact",
            )
        )

        capture = (
            tmp_path / ".punt-labs" / "quarry" / "captures" / "session-abcd1234.md"
        )
        content = capture.read_text(encoding="utf-8")
        assert "/Users/" not in content
        assert "@" not in content
        assert "~/repo" in content


class TestAsDir:
    """A payload cwd is honored only when it names an absolute path."""

    def test_absolute_path_is_returned(self) -> None:
        assert HookPayload.as_dir("/projects/myapp") == "/projects/myapp"

    def test_relative_path_is_unregistered(self) -> None:
        assert HookPayload.as_dir("src") == ""
        assert HookPayload.as_dir("..") == ""

    def test_non_string_is_unregistered(self) -> None:
        assert HookPayload.as_dir(None) == ""
        assert HookPayload.as_dir(123) == ""

    def test_blank_string_is_unregistered(self) -> None:
        assert HookPayload.as_dir("") == ""


class TestCwdHardeningSessionStart:
    """SessionStart must not auto-register on a non-absolute or non-string cwd.

    A relative cwd resolves against the hook process's own directory, so honoring
    it would register (and sync) an unintended tree — symmetric with the daemon
    covering-collection guard.
    """

    def test_relative_cwd_does_not_register(self, tmp_path: Path) -> None:
        with (
            patch("quarry.hooks._SessionStartContext._resolve_settings") as settings,
            patch("quarry.hooks.SyncLock.launch_background_sync") as sync,
        ):
            result = handle_session_start({"cwd": "src"})
        assert result == {}
        settings.assert_not_called()  # never reaches the registry
        sync.assert_not_called()

    def test_non_string_cwd_does_not_register(self, tmp_path: Path) -> None:
        with (
            patch("quarry.hooks._SessionStartContext._resolve_settings") as settings,
            patch("quarry.hooks.SyncLock.launch_background_sync") as sync,
        ):
            result = handle_session_start({"cwd": 123})
        assert result == {}
        settings.assert_not_called()
        sync.assert_not_called()


class TestCwdHardeningPostWebFetch:
    """A non-absolute cwd must not attribute a fetched page to a wrong project."""

    def test_relative_cwd_sends_empty_cwd(self) -> None:
        payload: dict[str, object] = {
            "cwd": "src",
            "tool_input": {"url": "https://example.com/docs"},
            "tool_response": json.dumps({"result": "<p>Some docs</p>"}),
        }
        with patch(
            "quarry.daemon_capture.DaemonCaptureSender.send_capture", return_value=True
        ) as cap:
            handle_post_web_fetch(payload)
        # cwd is blanked, so the daemon files into default-captures, not a
        # project derived from the hook process's own directory.
        assert cap.call_args[0][0].cwd == ""

    def test_non_string_cwd_sends_empty_cwd(self) -> None:
        payload: dict[str, object] = {
            "cwd": 123,
            "tool_input": {"url": "https://example.com/docs"},
            "tool_response": json.dumps({"result": "<p>Some docs</p>"}),
        }
        with patch(
            "quarry.daemon_capture.DaemonCaptureSender.send_capture", return_value=True
        ) as cap:
            handle_post_web_fetch(payload)
        assert cap.call_args[0][0].cwd == ""


class TestCwdHardeningPreCompact:
    """A non-absolute cwd must not write the capture relative to the process cwd."""

    def test_relative_cwd_skips_local_write(self, tmp_path: Path) -> None:
        transcript = _make_transcript(tmp_path, "Working somewhere")
        with (
            patch(
                "quarry.session_transcript.Path.home", return_value=tmp_path / "home"
            ),
            patch("quarry.capture.CaptureWriter.write") as write_local,
            patch("quarry.ethos_handle.EthosConfig.agent_handle_at") as ethos,
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as cap,
        ):
            handle_pre_compact(
                {
                    "cwd": "src",
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )
        # No local .md write and no ethos lookup rooted at the process cwd.
        write_local.assert_not_called()
        ethos.assert_not_called()
        assert cap.call_args[0][0].cwd == ""

    def test_non_string_cwd_skips_local_write_and_blanks_cwd(
        self, tmp_path: Path
    ) -> None:
        transcript = _make_transcript(tmp_path, "Working somewhere")
        with (
            patch(
                "quarry.session_transcript.Path.home", return_value=tmp_path / "home"
            ),
            patch("quarry.capture.CaptureWriter.write") as write_local,
            patch("quarry.ethos_handle.EthosConfig.agent_handle_at") as ethos,
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as cap,
        ):
            handle_pre_compact(
                {
                    "cwd": 123,
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )
        write_local.assert_not_called()
        ethos.assert_not_called()
        assert cap.call_args[0][0].cwd == ""
