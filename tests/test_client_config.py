"""Tests for ClientConfig — daemon-target resolution and the loopback bearer.

The security-critical properties: a loopback target reads serve.token LIVE (not
the stale stored token), a remote target keeps its stored bearer, and a missing
loopback token fails closed with a typed error rather than a silent tokenless
config.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quarry.client import ClientConfig, ClientConfigError


@contextmanager
def _run_dir_at(tmp_path: Path) -> Generator[None]:
    """Patch Settings so the run dir (serve.token home) is ``tmp_path``."""
    fake_settings = MagicMock()
    fake_settings.lancedb_path = tmp_path / "lancedb"  # parent == tmp_path
    with patch("quarry.client.config.Settings") as started:
        started.load.return_value.resolve_db_paths.return_value = fake_settings
        started.read_default_db.return_value = None
        yield


class TestFromLoginLoopback:
    def test_loopback_reads_live_serve_token(self, tmp_path: Path) -> None:
        (tmp_path / "serve.token").write_text("live-token")
        with _run_dir_at(tmp_path):
            cfg = ClientConfig.from_login(
                {"url": "wss://localhost:8420/mcp", "ca_cert": "/ca.crt"}
            )
        resolved = cfg.token
        assert resolved == "live-token"
        assert cfg.ca_cert == "/ca.crt"
        assert cfg.is_loopback is True

    def test_loopback_ignores_stale_stored_bearer(self, tmp_path: Path) -> None:
        # A stored bearer for a loopback target must be ignored — serve.token
        # rotates each restart, so only the live file is trusted.
        (tmp_path / "serve.token").write_text("live-token")
        with _run_dir_at(tmp_path):
            cfg = ClientConfig.from_login(
                {
                    "url": "wss://127.0.0.1:8420/mcp",
                    "headers": {"Authorization": "Bearer stale-stored"},
                }
            )
        resolved = cfg.token
        assert resolved == "live-token"

    def test_loopback_missing_token_fails_closed(self, tmp_path: Path) -> None:
        # No serve.token -> raise, never a tokenless config that 401s downstream.
        with (
            _run_dir_at(tmp_path),
            pytest.raises(ClientConfigError, match="quarryd is not running"),
        ):
            ClientConfig.from_login({"url": "wss://localhost:8420/mcp"})

    def test_loopback_unreadable_token_fails_closed(self, tmp_path: Path) -> None:
        # OSError breadth: a PermissionError on another UID's 0600 token must
        # surface ClientConfigError, not a raw OSError from deep in the call.
        with (
            _run_dir_at(tmp_path),
            patch(
                "quarry.run_dir.ServeTokenFile.read",
                side_effect=PermissionError("denied"),
            ),
            pytest.raises(ClientConfigError, match="could not be read"),
        ):
            ClientConfig.from_login({"url": "wss://localhost:8420/mcp"})

    def test_loopback_empty_token_fails_closed(self, tmp_path: Path) -> None:
        # A present-but-empty/corrupt token is not a credential: fail closed
        # rather than send an empty ``Authorization: Bearer``.
        (tmp_path / "serve.token").write_text("")
        with (
            _run_dir_at(tmp_path),
            pytest.raises(ClientConfigError, match="empty"),
        ):
            ClientConfig.from_login({"url": "wss://localhost:8420/mcp"})


class TestFromLoginRemote:
    def test_remote_keeps_stored_bearer(self) -> None:
        cfg = ClientConfig.from_login(
            {
                "url": "wss://quarry.example.com:8420/mcp",
                "ca_cert": "/ca.crt",
                "headers": {"Authorization": "Bearer remote-key"},
            }
        )
        resolved = cfg.token
        assert resolved == "remote-key"
        assert cfg.is_loopback is False

    def test_remote_without_headers_has_no_token(self) -> None:
        cfg = ClientConfig.from_login({"url": "wss://quarry.example.com:8420/mcp"})
        assert cfg.token is None

    def test_remote_does_not_read_serve_token(self, tmp_path: Path) -> None:
        # A remote target must never touch the local run dir.
        (tmp_path / "serve.token").write_text("local-token")
        with _run_dir_at(tmp_path):
            cfg = ClientConfig.from_login(
                {
                    "url": "wss://10.0.0.5:8420/mcp",
                    "headers": {"Authorization": "Bearer remote-key"},
                }
            )
        resolved = cfg.token
        assert resolved == "remote-key"


class TestRemoteMapping:
    def test_url_only(self) -> None:
        mapping = ClientConfig("ws://x:1", None, None).remote_mapping()
        assert mapping == {"url": "ws://x:1"}

    def test_url_and_ca(self) -> None:
        mapping = ClientConfig("wss://x:1", "/ca.crt", None).remote_mapping()
        assert mapping == {"url": "wss://x:1", "ca_cert": "/ca.crt"}

    def test_url_ca_and_bearer(self) -> None:
        mapping = ClientConfig("wss://x:1", "/ca.crt", "tok").remote_mapping()
        assert mapping == {
            "url": "wss://x:1",
            "ca_cert": "/ca.crt",
            "headers": {"Authorization": "Bearer tok"},
        }


class TestBearerExtraction:
    def test_malformed_authorization_is_none(self) -> None:
        cfg = ClientConfig.from_login(
            {"url": "wss://x.example.com:1", "headers": {"Authorization": "Basic z"}}
        )
        assert cfg.token is None

    def test_non_mapping_headers_is_none(self) -> None:
        cfg = ClientConfig.from_login(
            {"url": "wss://x.example.com:1", "headers": "not-a-dict"}
        )
        assert cfg.token is None

    def test_empty_bearer_is_none_and_emits_no_header(self) -> None:
        # A bare "Bearer " (empty/whitespace credential) is absent, not a token:
        # no Authorization header is emitted, mirroring the loopback fail-closed.
        cfg = ClientConfig.from_login(
            {"url": "wss://x.example.com:1", "headers": {"Authorization": "Bearer "}}
        )
        assert cfg.token is None
        assert "headers" not in cfg.remote_mapping()

    def test_whitespace_bearer_is_none(self) -> None:
        cfg = ClientConfig.from_login(
            {"url": "wss://x.example.com:1", "headers": {"Authorization": "Bearer    "}}
        )
        assert cfg.token is None

    def test_lowercase_bearer_scheme_is_accepted(self) -> None:
        # The daemon compares the scheme case-insensitively (parts[0].lower()
        # == "bearer"); a stored "bearer <tok>" must resolve to the token, not
        # be dropped client-side and sent tokenless (401).
        resolved = ClientConfig.from_login(
            {"url": "wss://x.example.com:1", "headers": {"Authorization": "bearer tok"}}
        ).token
        assert resolved == "tok"

    def test_uppercase_bearer_scheme_is_accepted(self) -> None:
        resolved = ClientConfig.from_login(
            {"url": "wss://x.example.com:1", "headers": {"Authorization": "BEARER tok"}}
        ).token
        assert resolved == "tok"


class TestLoopbackTokenProbe:
    """The non-raising probe helpers used by login validation and `--ping`."""

    def test_loopback_host_returns_live_token(self, tmp_path: Path) -> None:
        (tmp_path / "serve.token").write_text("live-probe-token")
        with _run_dir_at(tmp_path):
            resolved = ClientConfig.loopback_token("localhost")
        assert resolved == "live-probe-token"

    def test_loopback_host_missing_token_returns_none(self, tmp_path: Path) -> None:
        # Non-raising: a down daemon (no token) makes the probe report
        # unreachable from the connection, not fail closed here.
        with _run_dir_at(tmp_path):
            assert ClientConfig.loopback_token("127.0.0.1") is None

    def test_non_loopback_host_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "serve.token").write_text("local-token")
        with _run_dir_at(tmp_path):
            assert ClientConfig.loopback_token("quarry.example.com") is None

    def test_for_url_resolves_loopback(self, tmp_path: Path) -> None:
        (tmp_path / "serve.token").write_text("live-probe-token")
        with _run_dir_at(tmp_path):
            resolved = ClientConfig.loopback_token_for_url("wss://localhost:8420/mcp")
        assert resolved == "live-probe-token"

    def test_for_url_non_loopback_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "serve.token").write_text("local-token")
        with _run_dir_at(tmp_path):
            token = ClientConfig.loopback_token_for_url("wss://h.example.com:8420")
        assert token is None


class TestActiveDbRunDir:
    """serve.token resolves from the process's ACTIVE database's run dir."""

    def test_serve_token_uses_active_db_not_default(self, tmp_path: Path) -> None:
        (tmp_path / "work").mkdir()
        (tmp_path / "work" / "serve.token").write_text("work-db-token")
        (tmp_path / "default").mkdir()
        (tmp_path / "default" / "serve.token").write_text("default-db-token")

        def resolve(db: str | None) -> MagicMock:
            name = db or "default"
            fake = MagicMock()
            fake.lancedb_path = tmp_path / name / "lancedb"  # parent == tmp_path/name
            return fake

        with patch("quarry.client.config.Settings") as mock_settings:
            mock_settings.load.return_value.resolve_db_paths.side_effect = resolve
            mock_settings.active_db.return_value = "work"  # quarryd --db work
            cfg = ClientConfig.from_login({"url": "wss://localhost:8420/mcp"})

        # Reads the --db daemon's token, NOT the default database's.
        resolved = cfg.token
        assert resolved == "work-db-token"

    def test_serve_token_falls_back_to_default_db(self, tmp_path: Path) -> None:
        (tmp_path / "default").mkdir()
        (tmp_path / "default" / "serve.token").write_text("default-db-token")

        def resolve(db: str | None) -> MagicMock:
            name = db or "default"
            fake = MagicMock()
            fake.lancedb_path = tmp_path / name / "lancedb"
            return fake

        with patch("quarry.client.config.Settings") as mock_settings:
            mock_settings.load.return_value.resolve_db_paths.side_effect = resolve
            mock_settings.active_db.return_value = None  # no --db override
            cfg = ClientConfig.from_login({"url": "wss://127.0.0.1:8420/mcp"})

        resolved = cfg.token
        assert resolved == "default-db-token"
