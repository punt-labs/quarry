"""Session-setup regression tests (quarry-ridg round-2 Copilot findings).

Four separately failing behaviors:

* ``_allow_mcp_tools`` used to write ``mcp__plugin_quarry_quarry__*`` — the
  retired proxy namespace disconnected in quarry-ydym (PR #493).  The native
  server exposes tools under ``mcp__quarry__*`` / ``mcp__quarry-dev__*``; the
  old allow-list entry granted nothing and left every native call subject to
  a permission prompt.
* ``_read_plugin_name`` used ``data["name"]`` on a JSON document that may be
  a list or map an unexpected type for ``name``.  A ``TypeError`` or
  ``AttributeError`` slipped past ``open``'s ``(OSError, KeyError, ValueError)``
  catcher and broke the fail-open contract.
* ``handle_session_setup`` deferred ``mark_config`` / ``mark_payload`` until
  after the ``CLAUDE_PLUGIN_ROOT`` and ``open`` checks, so early-skip
  breadcrumbs rendered ``config=?, payload_ok=?`` instead of the honest
  ``config=on, payload_ok=Y`` those paths deserve.
* ``_read_plugin_name`` accepted the plugin name verbatim into a
  ``mcp__<name>__*`` wildcard grant.  A hostile ``plugin.json`` carrying
  ``*``, whitespace, ``/``, or ``.`` in the name could broaden the grant
  beyond the plugin's own tools or malform the entry so it never matches.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from quarry._stdlib import _SessionSetup, handle_session_setup

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``Path.home()`` at a fresh tmp_path with an empty settings.json."""
    monkeypatch.setenv("HOME", str(tmp_path))
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text("{}\n")
    yield tmp_path


def _make_plugin(root: Path, manifest: object) -> Path:
    """Write ``manifest`` as ``root/.claude-plugin/plugin.json``."""
    plugin_dir = root / ".claude-plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest))
    return root


class TestAllowMcpTools:
    """``_allow_mcp_tools`` grants the native mcp__<plugin>__ namespace."""

    def test_grants_native_namespace(self, fake_home: Path, tmp_path: Path) -> None:
        plugin_root = _make_plugin(tmp_path / "plugin", {"name": "quarry"})
        setup = _SessionSetup.open(plugin_root)
        assert setup is not None
        setup._allow_mcp_tools()

        settings = json.loads((fake_home / ".claude" / "settings.json").read_text())
        allow = settings["permissions"]["allow"]
        assert "mcp__quarry__*" in allow

    def test_does_not_grant_retired_proxy_namespace(
        self, fake_home: Path, tmp_path: Path
    ) -> None:
        plugin_root = _make_plugin(tmp_path / "plugin", {"name": "quarry"})
        setup = _SessionSetup.open(plugin_root)
        assert setup is not None
        setup._allow_mcp_tools()

        settings = json.loads((fake_home / ".claude" / "settings.json").read_text())
        allow = settings["permissions"]["allow"]
        assert not any("plugin_quarry" in entry for entry in allow)

    def test_dev_plugin_grants_dev_namespace(
        self, fake_home: Path, tmp_path: Path
    ) -> None:
        plugin_root = _make_plugin(tmp_path / "plugin", {"name": "quarry-dev"})
        setup = _SessionSetup.open(plugin_root)
        assert setup is not None
        setup._allow_mcp_tools()

        settings = json.loads((fake_home / ".claude" / "settings.json").read_text())
        allow = settings["permissions"]["allow"]
        assert "mcp__quarry-dev__*" in allow

    def test_grants_wildcard_when_narrower_entry_exists(
        self, fake_home: Path, tmp_path: Path
    ) -> None:
        """A pre-seeded narrower entry must not shadow the wildcard.

        Guards against a substring-match check that mistakes any string
        containing ``mcp__<plugin>__`` (e.g. a single-tool entry) for the
        wildcard already being present, leaving native tools prompt-gated.
        """
        settings_path = fake_home / ".claude" / "settings.json"
        settings_path.write_text(
            json.dumps({"permissions": {"allow": ["mcp__quarry__some_specific_tool"]}})
        )
        plugin_root = _make_plugin(tmp_path / "plugin", {"name": "quarry"})
        setup = _SessionSetup.open(plugin_root)
        assert setup is not None
        setup._allow_mcp_tools()

        allow = json.loads(settings_path.read_text())["permissions"]["allow"]
        assert "mcp__quarry__*" in allow


class TestOpenFailOpen:
    """``_SessionSetup.open`` returns ``None`` for any structural defect."""

    def test_returns_none_when_name_is_null(self, tmp_path: Path) -> None:
        plugin_root = _make_plugin(tmp_path / "plugin", {"name": None})
        assert _SessionSetup.open(plugin_root) is None

    def test_returns_none_when_manifest_is_list(self, tmp_path: Path) -> None:
        plugin_root = _make_plugin(tmp_path / "plugin", ["not", "a", "dict"])
        assert _SessionSetup.open(plugin_root) is None

    def test_returns_none_when_name_missing(self, tmp_path: Path) -> None:
        plugin_root = _make_plugin(tmp_path / "plugin", {"other": "field"})
        assert _SessionSetup.open(plugin_root) is None

    def test_returns_none_when_manifest_absent(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        assert _SessionSetup.open(plugin_root) is None

    def test_returns_none_when_manifest_is_invalid_json(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "plugin"
        (plugin_root / ".claude-plugin").mkdir(parents=True)
        (plugin_root / ".claude-plugin" / "plugin.json").write_text("{not json")
        assert _SessionSetup.open(plugin_root) is None

    def test_returns_setup_for_valid_manifest(self, tmp_path: Path) -> None:
        plugin_root = _make_plugin(tmp_path / "plugin", {"name": "quarry"})
        setup = _SessionSetup.open(plugin_root)
        assert setup is not None


class TestPluginNameSlugValidation:
    """``_read_plugin_name`` accepts only conservative-slug names.

    Names flow into ``settings.json`` as ``mcp__<name>__*`` wildcard grants;
    an unsafe character (``*``, ``/``, ``.``, whitespace, newline, ``[``, etc.)
    could broaden the grant beyond the plugin's own tools or malform the
    entry so it never matches.  ``open`` catches the ``ValueError`` and
    returns ``None`` — the fail-open path already exercised in round 2.
    """

    @pytest.mark.parametrize(
        "name",
        ["quarry", "quarry-dev", "my_tool_v42", "A", "abc123", "x" * 64],
    )
    def test_accepts_safe_slugs(self, tmp_path: Path, name: str) -> None:
        plugin_root = _make_plugin(tmp_path / "plugin", {"name": name})
        setup = _SessionSetup.open(plugin_root)
        assert setup is not None
        assert setup._plugin_name == name

    @pytest.mark.parametrize(
        ("name", "reason"),
        [
            ("*", "wildcard"),
            ("foo/bar", "path separator"),
            ("foo bar", "internal whitespace"),
            ("foo.bar", "dot"),
            ("foo\tbar", "tab"),
            ("foo\nbar", "newline"),
            ("[foo]", "brackets"),
            ("", "empty"),
            ("-foo", "leading hyphen"),
            ("_foo", "leading underscore"),
            ("x" * 65, "65 chars, over the 64 cap"),
            ("foo bar*", "wildcard hidden after whitespace"),
        ],
    )
    def test_rejects_unsafe_slugs_via_open(
        self, tmp_path: Path, name: str, reason: str
    ) -> None:
        del reason
        plugin_root = _make_plugin(tmp_path / "plugin", {"name": name})
        assert _SessionSetup.open(plugin_root) is None

    def test_read_plugin_name_raises_value_error_on_unsafe(
        self, tmp_path: Path
    ) -> None:
        plugin_root = _make_plugin(tmp_path / "plugin", {"name": "foo*"})
        with pytest.raises(ValueError, match="safe slug"):
            _SessionSetup._read_plugin_name(plugin_root)

    def test_unsafe_name_leaves_settings_untouched(
        self, fake_home: Path, tmp_path: Path
    ) -> None:
        """An unsafe manifest must not reach ``permissions.allow`` at all.

        Guards against a slip where ``open`` returns a setup for an unsafe
        name and ``dispatch`` writes ``mcp__foo*__*`` — the exact hostile
        wildcard the slug validator exists to block.
        """
        settings_path = fake_home / ".claude" / "settings.json"
        original = settings_path.read_text()
        plugin_root = _make_plugin(tmp_path / "plugin", {"name": "foo*"})
        assert _SessionSetup.open(plugin_root) is None
        assert settings_path.read_text() == original


class TestSessionSetupBreadcrumb:
    """``handle_session_setup`` marks config/payload before every skip path."""

    @pytest.fixture
    def caplog_hooks(
        self, caplog: pytest.LogCaptureFixture
    ) -> pytest.LogCaptureFixture:
        caplog.set_level(logging.INFO, logger="quarry.hooks")
        return caplog

    def test_no_plugin_root_breadcrumb_marks_config_and_payload(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog_hooks: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        handle_session_setup({})
        entered = [r for r in caplog_hooks.records if "entered" in r.getMessage()]
        assert entered, "session-setup emitted no entered breadcrumb"
        msg = entered[-1].getMessage()
        assert "config=on" in msg
        assert "payload_ok=Y" in msg
        assert "skip:no-CLAUDE_PLUGIN_ROOT" in msg

    def test_bad_plugin_root_breadcrumb_marks_config_and_payload(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog_hooks: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "does-not-exist"))
        handle_session_setup({})
        entered = [r for r in caplog_hooks.records if "entered" in r.getMessage()]
        assert entered
        msg = entered[-1].getMessage()
        assert "config=on" in msg
        assert "payload_ok=Y" in msg
        assert "skip:not-a-dir" in msg

    def test_dispatch_oserror_emits_error_breadcrumb_and_reraises(
        self,
        fake_home: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog_hooks: pytest.LogCaptureFixture,
    ) -> None:
        """A filesystem fault inside ``dispatch()`` must trace before re-raising.

        Without the guard, an ``OSError`` from ``shutil.copy2`` propagates past
        the ``HookTrace``, run_hook prints ``{}`` fail-open, and ``quarry.log``
        holds no record of what went wrong -- the G6 gap for error paths.
        """
        plugin_root = _make_plugin(tmp_path / "plugin", {"name": "quarry"})
        commands_dir = plugin_root / "commands"
        commands_dir.mkdir()
        (commands_dir / "sample.md").write_text("# sample\n")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        def _raise(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("quarry._stdlib.shutil.copy2", _raise)

        with pytest.raises(OSError, match="disk full"):
            handle_session_setup({})

        entered = [r for r in caplog_hooks.records if "entered" in r.getMessage()]
        assert entered, "session-setup emitted no entered breadcrumb"
        msg = entered[-1].getMessage()
        assert "config=on" in msg
        assert "payload_ok=Y" in msg
        assert "error:dispatch-failed:OSError" in msg
