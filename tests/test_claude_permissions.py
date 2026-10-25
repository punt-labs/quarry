"""Tests for quarry's allow rules in a project's ``.claude/settings.json`` (#552)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from quarry.claude_permissions import QUARRY_RULES, ProjectPermissions

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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


class TestRuleSet:
    def test_covers_native_mcp_namespace_and_each_command(self) -> None:
        assert QUARRY_RULES[0] == "mcp__quarry__*"
        assert "Skill(find)" in QUARRY_RULES
        assert "Skill(ingest)" in QUARRY_RULES
        assert not any("plugin_quarry" in rule for rule in QUARRY_RULES)
        assert not any(rule.endswith("-dev)") for rule in QUARRY_RULES)


class TestGrant:
    def test_creates_settings_when_absent(self, tmp_path: Path) -> None:
        change = ProjectPermissions(tmp_path).grant()

        assert change.error == ""
        assert change.changed == QUARRY_RULES
        assert _allow(tmp_path) == list(QUARRY_RULES)

    def test_preserves_unrelated_settings_and_rule_order(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            {
                "theme": "dark",
                "permissions": {
                    "allow": ["Bash(make:*)", "Skill(find)"],
                    "deny": ["X"],
                },
            },
        )

        change = ProjectPermissions(tmp_path).grant()

        data = json.loads(_settings(tmp_path).read_text())
        assert data["theme"] == "dark"
        assert data["permissions"]["deny"] == ["X"]
        allow = data["permissions"]["allow"]
        assert allow[:2] == ["Bash(make:*)", "Skill(find)"]
        assert allow.count("Skill(find)") == 1
        assert "Skill(find)" not in change.changed
        assert set(allow) == {"Bash(make:*)", *QUARRY_RULES}

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

        change = ProjectPermissions(root).grant()

        assert change.error
        assert (outside / "settings.json").read_text() == "{}\n"

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

        assert change.changed == QUARRY_RULES
        assert _allow(tmp_path) == ["Bash(make:*)", "Skill(other)"]

    def test_revoke_without_settings_writes_nothing(self, tmp_path: Path) -> None:
        change = ProjectPermissions(tmp_path).revoke()

        assert change.changed == ()
        assert change.error == ""
        assert not _settings(tmp_path).exists()

    def test_revoke_without_quarry_rules_leaves_file_untouched(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path, {"theme": "dark"})
        text = path.read_text()

        change = ProjectPermissions(tmp_path).revoke()

        assert change.changed == ()
        assert path.read_text() == text
