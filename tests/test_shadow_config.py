"""Tests for ShadowConfig: frontmatter parsing, absence contract, remote derivation."""

from __future__ import annotations

from pathlib import Path

from quarry.shadow.config import ShadowConfig

_CONFIG_REL = Path(".punt-labs") / "quarry" / "config.md"


def _write_config(directory: Path, body: str) -> None:
    path = directory / _CONFIG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestFromProject:
    def test_none_when_no_config_file(self, tmp_path: Path) -> None:
        assert ShadowConfig.from_project(tmp_path) is None

    def test_none_when_no_shadow_block(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "---\nauto_capture:\n  web_fetch: true\n---\n")
        assert ShadowConfig.from_project(tmp_path) is None

    def test_parses_enabled_and_remote(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "---\nshadow:\n  enabled: true\n  remote: git@h:o/r-quarry.git\n---\n",
        )
        config = ShadowConfig.from_project(tmp_path)
        assert config is not None
        assert config.enabled is True
        assert config.remote == "git@h:o/r-quarry.git"
        assert config.acknowledge_unverified is False

    def test_empty_block_uses_defaults(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "---\nshadow:\n  enabled: false\n---\n")
        config = ShadowConfig.from_project(tmp_path)
        assert config is not None
        assert config.enabled is False
        assert config.remote == ""

    def test_acknowledge_unverified_parsed(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "---\nshadow:\n  enabled: true\n  acknowledge_unverified: yes\n---\n",
        )
        config = ShadowConfig.from_project(tmp_path)
        assert config is not None
        assert config.acknowledge_unverified is True

    def test_quoted_remote_is_unquoted(self, tmp_path: Path) -> None:
        _write_config(tmp_path, '---\nshadow:\n  remote: "git@h:o/r.git"\n---\n')
        config = ShadowConfig.from_project(tmp_path)
        assert config is not None
        assert config.remote == "git@h:o/r.git"

    def test_malformed_config_returns_none(self, tmp_path: Path) -> None:
        # No frontmatter fences at all -> no shadow block -> None (no crash).
        _write_config(tmp_path, "not yaml at all\nshadow: enabled\n")
        assert ShadowConfig.from_project(tmp_path) is None

    def test_unrecognized_bool_fails_closed(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "---\nshadow:\n  enabled: maybe\n---\n")
        config = ShadowConfig.from_project(tmp_path)
        assert config is not None
        assert config.enabled is False


class TestDeriveRemote:
    def test_ssh_remote(self) -> None:
        assert (
            ShadowConfig.derive_remote("git@github.com:org/repo.git")
            == "git@github.com:org/repo-quarry.git"
        )

    def test_https_remote(self) -> None:
        assert (
            ShadowConfig.derive_remote("https://github.com/org/repo.git")
            == "https://github.com/org/repo-quarry.git"
        )

    def test_no_git_suffix(self) -> None:
        assert (
            ShadowConfig.derive_remote("git@github.com:org/repo")
            == "git@github.com:org/repo-quarry"
        )

    def test_empty_origin_yields_empty(self) -> None:
        assert ShadowConfig.derive_remote("") == ""
        assert ShadowConfig.derive_remote("   ") == ""
