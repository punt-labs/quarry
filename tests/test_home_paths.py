"""QuarryHome: paths under ~ are resolved when asked, never at import."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from quarry.home_paths import HomeUnresolvedError, QuarryHome


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

    with pytest.raises(HomeUnresolvedError, match="home directory"):
        QuarryHome.tls_dir()


@pytest.mark.parametrize("home", ["", "relative/home", "."])
def test_an_empty_or_relative_home_is_refused(
    home: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: Path(home))

    with pytest.raises(HomeUnresolvedError, match="not an absolute path"):
        QuarryHome.root()


def test_the_unresolved_error_is_chained_to_what_pathlib_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_home() -> Path:
        msg = "Could not determine home directory."
        raise RuntimeError(msg)

    monkeypatch.setattr(Path, "home", no_home)

    with pytest.raises(HomeUnresolvedError) as raised:
        QuarryHome.home()

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert isinstance(raised.value, RuntimeError)


def test_the_log_directory_follows_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert QuarryHome.log_root() == tmp_path / ".punt-labs" / "quarry" / "logs"


def test_a_tilde_path_expands_under_the_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert QuarryHome.expanded(Path("~/proj")) == tmp_path / "proj"


def test_a_tilde_path_with_no_home_raises_the_typed_error() -> None:
    with (
        patch("pathlib.Path.expanduser", side_effect=RuntimeError("no home")),
        pytest.raises(HomeUnresolvedError),
    ):
        QuarryHome.expanded(Path("~/proj"))


def test_a_path_without_a_tilde_is_returned_as_it_is() -> None:
    assert QuarryHome.expanded(Path("/srv/proj")) == Path("/srv/proj")
