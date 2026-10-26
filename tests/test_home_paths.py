"""QuarryHome: paths under ~ are resolved when asked, never at import."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.home_paths import QuarryHome


def test_every_path_follows_the_home_directory_at_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert QuarryHome.root() == tmp_path / ".punt-labs" / "quarry"
    assert QuarryHome.data_root() == QuarryHome.root() / "data"
    assert QuarryHome.tls_dir() == QuarryHome.root() / "tls"
    assert QuarryHome.daemon_ca() == QuarryHome.tls_dir() / "ca.crt"
    assert QuarryHome.env_file() == QuarryHome.root() / "quarry.env"
    assert QuarryHome.proxy_config() == tmp_path / ".punt-labs/mcp-proxy/quarry.toml"
    assert QuarryHome.claude_projects() == tmp_path / ".claude" / "projects"
    assert QuarryHome.launchd_plist("x") == (
        tmp_path / "Library" / "LaunchAgents" / "x.plist"
    )
    assert QuarryHome.systemd_unit() == (
        tmp_path / ".config" / "systemd" / "user" / "quarry.service"
    )


def test_a_home_that_cannot_be_resolved_fails_only_when_a_path_is_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_home() -> Path:
        msg = "Could not determine home directory."
        raise RuntimeError(msg)

    monkeypatch.setattr(Path, "home", no_home)

    with pytest.raises(RuntimeError, match="home directory"):
        QuarryHome.tls_dir()
