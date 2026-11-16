"""Tests for quarry's allow rules in a project's ``.claude/settings.json`` (#552)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quarry.claude_permissions import QUARRY_RULES, ProjectPermissions


def _settings(root: Path) -> Path:
    return root / ".claude" / "settings.json"


def _write(root: Path, data: object) -> Path:
    path = _settings(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def _allow(root: Path) -> list[object]:
    allow: list[object] = json.loads(_settings(root).read_text())["permissions"][
        "allow"
    ]
    return allow


def _ask(root: Path) -> list[object]:
    ask: list[object] = json.loads(_settings(root).read_text())["permissions"]["ask"]
    return ask


_ALLOW = QUARRY_RULES["allow"]
_ASK = QUARRY_RULES["ask"]
_ALL = (*_ALLOW, *_ASK)


class TestRuleSet:
    def test_allows_each_surface_broadly(self) -> None:
        assert _ALLOW == ("Bash(quarry:*)", "mcp__quarry__*", "Skill(quarry:*)")
        assert not any("plugin_quarry" in rule for rule in _ALL)
        assert not any("-dev" in rule for rule in _ALL)

    @pytest.mark.parametrize(
        "command",
        [
            "login",
            "logout",
            "remote",
            "install",
            "uninstall",
            "enable",
            "disable",
            "captures",
            "skills",
            "mcp",
            "delete",
            "use",
            "prune-mcp-shadow",
        ],
    )
    def test_admin_and_destructive_commands_ask(self, command: str) -> None:
        assert f"Bash(quarry {command}:*)" in _ASK

    def test_data_plane_commands_do_not_ask(self) -> None:
        for command in ("find", "ingest", "remember", "learn", "show", "status"):
            assert f"Bash(quarry {command}:*)" not in _ASK

    def test_no_catch_all_flag_rule(self) -> None:
        assert "Bash(quarry -*)" not in _ASK

    @pytest.mark.parametrize("verb", ["grant", "revoke"])
    def test_legacy_flag_rule_is_stripped(self, tmp_path: Path, verb: str) -> None:
        _write(tmp_path, {"permissions": {"ask": ["Bash(ls:*)", "Bash(quarry -*)"]}})
        getattr(ProjectPermissions(tmp_path), verb)()
        assert _ask(tmp_path) == ["Bash(ls:*)", *(_ASK if verb == "grant" else ())]

    def test_mcp_delete_asks(self) -> None:
        assert "mcp__quarry__delete" in _ASK


class TestGrant:
    def test_creates_settings_when_absent(self, tmp_path: Path) -> None:
        change = ProjectPermissions(tmp_path).grant()

        assert change.error == ""
        assert change.changed == _ALL
        assert _allow(tmp_path) == list(_ALLOW)
        assert _ask(tmp_path) == list(_ASK)

    def test_preserves_unrelated_settings_and_rule_order(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            {
                "theme": "dark",
                "permissions": {
                    "allow": ["Bash(make:*)", "Skill(quarry:*)"],
                    "deny": ["X"],
                },
            },
        )

        change = ProjectPermissions(tmp_path).grant()

        data = json.loads(_settings(tmp_path).read_text())
        assert data["theme"] == "dark"
        assert data["permissions"]["deny"] == ["X"]
        allow = data["permissions"]["allow"]
        assert allow[:2] == ["Bash(make:*)", "Skill(quarry:*)"]
        assert allow.count("Skill(quarry:*)") == 1
        assert "Skill(quarry:*)" not in change.changed
        assert set(allow) == {"Bash(make:*)", *_ALLOW}

    def test_narrower_mcp_entry_does_not_shadow_wildcard(self, tmp_path: Path) -> None:
        _write(tmp_path, {"permissions": {"allow": ["mcp__quarry__find"]}})

        ProjectPermissions(tmp_path).grant()

        assert "mcp__quarry__*" in _allow(tmp_path)

    def test_repeat_grant_is_a_no_write_noop(self, tmp_path: Path) -> None:
        ProjectPermissions(tmp_path).grant()
        before = _settings(tmp_path).stat().st_mtime_ns
        text = _settings(tmp_path).read_text()

        change = ProjectPermissions(tmp_path).grant()

        assert change.changed == ()
        assert change.error == ""
        assert _settings(tmp_path).read_text() == text
        assert _settings(tmp_path).stat().st_mtime_ns == before

    def test_invalid_json_is_reported_and_left_untouched(self, tmp_path: Path) -> None:
        path = _settings(tmp_path)
        path.parent.mkdir()
        path.write_text("{not json")

        change = ProjectPermissions(tmp_path).grant()

        assert change.changed == ()
        assert "not valid JSON" in change.error
        assert path.read_text() == "{not json"

    def test_non_object_settings_is_refused(self, tmp_path: Path) -> None:
        path = _write(tmp_path, ["a"])

        change = ProjectPermissions(tmp_path).grant()

        assert "not a JSON object" in change.error
        assert json.loads(path.read_text()) == ["a"]

    def test_wrong_shaped_allow_is_refused_not_overwritten(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path, {"permissions": {"allow": "Bash(ls)"}})

        change = ProjectPermissions(tmp_path).grant()

        assert "'permissions.allow' is not a list" in change.error
        assert json.loads(path.read_text()) == {"permissions": {"allow": "Bash(ls)"}}

    def test_wrong_shaped_permissions_is_refused(self, tmp_path: Path) -> None:
        _write(tmp_path, {"permissions": []})

        change = ProjectPermissions(tmp_path).grant()

        assert "'permissions' is not an object" in change.error

    def test_symlinked_settings_is_refused(self, tmp_path: Path) -> None:
        outside = tmp_path / "global.json"
        outside.write_text("{}\n")
        root = tmp_path / "repo"
        (root / ".claude").mkdir(parents=True)
        (root / ".claude" / "settings.json").symlink_to(outside)

        change = ProjectPermissions(root).grant()

        assert change.error
        assert change.changed == ()
        assert outside.read_text() == "{}\n"

    def test_symlinked_claude_dir_is_refused(self, tmp_path: Path) -> None:
        outside = tmp_path / "home-claude"
        outside.mkdir()
        (outside / "settings.json").write_text("{}\n")
        root = tmp_path / "repo"
        root.mkdir()
        (root / ".claude").symlink_to(outside)

        grant = ProjectPermissions(root).grant()
        revoke = ProjectPermissions(root).revoke()

        assert ".claude is a symlink" in grant.error
        assert ".claude is a symlink" in revoke.error
        assert sorted(p.name for p in outside.iterdir()) == ["settings.json"]
        assert (outside / "settings.json").read_text() == "{}\n"

    def test_takes_the_mandated_shared_lock(self, tmp_path: Path) -> None:
        ProjectPermissions(tmp_path).grant()

        assert (tmp_path / ".claude" / ".settings.json.punt-import.lock").is_file()
        assert not (tmp_path / ".claude" / ".settings.json.lock").exists()

    def test_home_directory_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))

        change = ProjectPermissions(tmp_path).grant()

        assert "home directory" in change.error
        assert not _settings(tmp_path).exists()


class TestRevoke:
    def test_removes_only_quarry_rules(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            {
                "permissions": {
                    "allow": ["Bash(make:*)", "mcp__quarry__*", "Skill(other)"]
                }
            },
        )
        ProjectPermissions(tmp_path).grant()

        change = ProjectPermissions(tmp_path).revoke()

        assert change.changed == _ALL
        assert _allow(tmp_path) == ["Bash(make:*)", "Skill(other)"]
        assert _ask(tmp_path) == []

    def test_revoke_without_settings_writes_nothing(self, tmp_path: Path) -> None:
        change = ProjectPermissions(tmp_path).revoke()

        assert change.changed == ()
        assert change.error == ""
        assert not (tmp_path / ".claude").exists()

    def test_unsearchable_claude_dir_is_reported_not_raised(
        self, tmp_path: Path
    ) -> None:
        claude = tmp_path / ".claude"
        claude.mkdir()
        claude.chmod(0)
        try:
            change = ProjectPermissions(tmp_path).revoke()
        finally:
            claude.chmod(0o755)

        assert change.changed == ()
        assert change.error

    def test_revoke_without_quarry_rules_leaves_file_untouched(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path, {"theme": "dark"})
        text = path.read_text()

        change = ProjectPermissions(tmp_path).revoke()

        assert change.changed == ()
        assert path.read_text() == text
