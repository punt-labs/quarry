"""Tests for ClientConfig — daemon-target resolution and the loopback bearer.

The security-critical properties: a LITERAL-loopback-IP target reads serve.token
LIVE (not the stale stored token); an ambiguous NAME (``localhost``) is NOT a
presentation target (a resolver could redirect it to a co-tenant), so its stored
bearer stands; a remote target keeps its stored bearer; and a missing loopback
token fails closed with a typed error rather than a silent tokenless config.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quarry.client import ClientConfig, ClientConfigError
from quarry.net import LoopbackPolicy


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
                {"url": "wss://127.0.0.1:8420/mcp", "ca_cert": "/ca.crt"}
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
            ClientConfig.from_login({"url": "wss://127.0.0.1:8420/mcp"})

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
            ClientConfig.from_login({"url": "wss://127.0.0.1:8420/mcp"})

    def test_corrupt_default_db_config_fails_closed(self, tmp_path: Path) -> None:
        # Exception-boundary breadth: resolve_db_paths() raises ValueError on a
        # default-db name with a path separator (e.g. a corrupt
        # default.database="../evil").  That resolution now runs INSIDE the try,
        # so from_login fails closed with ClientConfigError, not a raw ValueError.
        with (
            patch("quarry.client.config.Settings") as mock_settings,
            pytest.raises(ClientConfigError, match="could not be read"),
        ):
            mock_settings.active_db.return_value = "../evil"
            mock_settings.load.return_value.resolve_db_paths.side_effect = ValueError(
                "db name contains a path separator"
            )
            ClientConfig.from_login({"url": "wss://127.0.0.1:8420/mcp"})

    def test_unreadable_default_db_config_fails_closed(self, tmp_path: Path) -> None:
        # active_db() can raise OSError on an unreadable default-db config; it too
        # runs inside the try, so from_login fails closed, not a raw OSError.
        with (
            patch("quarry.client.config.Settings") as mock_settings,
            pytest.raises(ClientConfigError, match="could not be read"),
        ):
            mock_settings.active_db.side_effect = OSError("default-db unreadable")
            ClientConfig.from_login({"url": "wss://127.0.0.1:8420/mcp"})

    def test_stored_localhost_auto_migrates_to_literal(self, tmp_path: Path) -> None:
        # A config stored before the literal-IP flip holds the NAME ``localhost``.
        # On READ it must auto-migrate to 127.0.0.1 so the operator is not locked
        # out — the live serve.token resolves AND the resolved target is the
        # literal, never the ambiguous name.
        (tmp_path / "serve.token").write_text("live-token")
        with _run_dir_at(tmp_path):
            cfg = ClientConfig.from_login({"url": "wss://localhost:8420/mcp"})
        resolved = cfg.token
        assert resolved == "live-token"  # (a) live token, no re-login
        assert cfg.url == "wss://127.0.0.1:8420/mcp"  # (a) target is the literal
        assert cfg.remote_mapping()["url"] == "wss://127.0.0.1:8420/mcp"
        assert cfg.is_loopback is True

    def test_stored_localhost_never_connects_to_raw_name(self, tmp_path: Path) -> None:
        # (b) The exfiltration stays closed: the resolved target must be the
        # literal 127.0.0.1, never a raw "localhost" a resolver could redirect
        # to a co-tenant's ::1.  Assert the name appears nowhere in the target.
        (tmp_path / "serve.token").write_text("live-token")
        with _run_dir_at(tmp_path):
            cfg = ClientConfig.from_login({"url": "wss://localhost:8420/mcp"})
        assert "localhost" not in cfg.url
        assert "localhost" not in str(cfg.remote_mapping()["url"])

    def test_stored_localhost_ignores_stale_stored_bearer(self, tmp_path: Path) -> None:
        # A migrated localhost target reads the LIVE serve.token, never the
        # stale stored bearer (which rotates each daemon restart).
        (tmp_path / "serve.token").write_text("live-token")
        with _run_dir_at(tmp_path):
            cfg = ClientConfig.from_login(
                {
                    "url": "wss://localhost:8420/mcp",
                    "headers": {"Authorization": "Bearer stale-stored"},
                }
            )
        resolved = cfg.token
        assert resolved == "live-token"

    def test_loopback_empty_token_fails_closed(self, tmp_path: Path) -> None:
        # A present-but-empty/corrupt token is not a credential: fail closed
        # rather than send an empty ``Authorization: Bearer``.
        (tmp_path / "serve.token").write_text("")
        with (
            _run_dir_at(tmp_path),
            pytest.raises(ClientConfigError, match="empty"),
        ):
            ClientConfig.from_login({"url": "wss://127.0.0.1:8420/mcp"})


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

    def test_cleartext_ws_remote_login_is_refused(self) -> None:
        """A stored ws:// (plaintext) non-loopback login is refused: TLS-or-refused."""
        with pytest.raises(ClientConfigError, match="cleartext"):
            ClientConfig.from_login({"url": "ws://quarry.example.com:9000"})

    def test_cleartext_http_remote_login_is_refused(self) -> None:
        with pytest.raises(ClientConfigError, match="cleartext"):
            ClientConfig.from_login({"url": "http://10.0.0.5:8420"})

    def test_loopback_plaintext_login_is_allowed(self, tmp_path: Path) -> None:
        """Loopback plaintext (ws://127.0.0.1) is same-machine — never refused."""
        (tmp_path / "serve.token").write_text("loop-token")
        with _run_dir_at(tmp_path):
            cfg = ClientConfig.from_login({"url": "ws://127.0.0.1:8420"})
        resolved = cfg.token
        assert resolved == "loop-token"


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

    def test_tab_separated_bearer_resolves(self) -> None:
        # The daemon parses Authorization with split() (any whitespace); a
        # tab-separated "Bearer\ttok" must resolve to the token, not be dropped
        # client-side (partition(" ") left the scheme glued to the token).
        resolved = ClientConfig.from_login(
            {
                "url": "wss://x.example.com:1",
                "headers": {"Authorization": "Bearer\ttok"},
            }
        ).token
        assert resolved == "tok"

    def test_more_than_two_parts_is_rejected(self) -> None:
        # The daemon 401s a >2-part header (len(parts) != 2), so the client must
        # not present one: a token with an embedded space is not a valid bearer.
        cfg = ClientConfig.from_login(
            {
                "url": "wss://x.example.com:1",
                "headers": {"Authorization": "Bearer tok extra"},
            }
        )
        assert cfg.token is None
        assert "headers" not in cfg.remote_mapping()


class TestLoopbackTokenProbe:
    """The non-raising probe helpers used by login validation and `--ping`."""

    def test_loopback_host_returns_live_token(self, tmp_path: Path) -> None:
        (tmp_path / "serve.token").write_text("live-probe-token")
        with _run_dir_at(tmp_path):
            resolved = ClientConfig.loopback_token("127.0.0.1")
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
            resolved = ClientConfig.loopback_token_for_url("wss://127.0.0.1:8420/mcp")
        assert resolved == "live-probe-token"

    def test_for_url_non_loopback_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "serve.token").write_text("local-token")
        with _run_dir_at(tmp_path):
            token = ClientConfig.loopback_token_for_url("wss://h.example.com:8420")
        assert token is None

    def test_malformed_url_returns_none_without_reading_token(
        self, tmp_path: Path
    ) -> None:
        # Gate/connect agreement: canonical_url fails closed on a malformed URL
        # (wss://localhost:bad) — returns it unchanged with the NAME host.  The
        # gate derives the target from canonical_url, so it is NOT a literal
        # loopback: no live serve.token is read (assert token_file.read is never
        # called), and no token is presented to a resolver-controlled name.
        (tmp_path / "serve.token").write_text("live-token")
        with (
            _run_dir_at(tmp_path),
            patch(
                "quarry.run_dir.ServeTokenFile.read", return_value="live-token"
            ) as mock_read,
        ):
            token = ClientConfig.loopback_token_for_url("wss://localhost:bad/mcp")
        assert token is None
        assert mock_read.call_count == 0  # serve.token never touched

    def test_malformed_url_is_not_loopback_url(self) -> None:
        assert ClientConfig.is_loopback_url("wss://localhost:bad") is False

    def test_well_formed_loopback_name_reads_live_token(self, tmp_path: Path) -> None:
        # The fail-closed gate must not suppress the normal resolution: a
        # well-formed loopback-name URL migrates to 127.0.0.1 and reads the token.
        (tmp_path / "serve.token").write_text("live-token")
        with _run_dir_at(tmp_path):
            resolved = ClientConfig.loopback_token_for_url("wss://localhost:8420/mcp")
            eligible = ClientConfig.is_loopback_url("wss://localhost:8420/mcp")
        assert resolved == "live-token"
        assert eligible is True

    @pytest.mark.parametrize(
        "url",
        ["wss://127.0.0.1:8420/mcp", "wss://[::1]:8420/mcp"],
    )
    def test_literal_loopback_url_is_eligible(self, url: str) -> None:
        assert ClientConfig.is_loopback_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "wss://localhost:8420/mcp",
            "wss://127.0.0.1:8420/mcp",
            "wss://[::1]:8420/mcp",
            "ws://localhost/mcp",
            "wss://localhost:bad/mcp",
            "wss://gpu.example.com:8420/mcp",
        ],
    )
    def test_gate_and_connect_agree(self, url: str) -> None:
        # Invariant: whenever is_loopback_url says "eligible" (present the live
        # token), the host the connection actually targets — the host of
        # canonical_url(url) — IS a literal loopback IP.  Gate and connect can
        # never diverge, so the token is never presented to an ambiguous name.
        if ClientConfig.is_loopback_url(url):
            connect_host = ClientConfig._host_of(ClientConfig.canonical_url(url))
            assert LoopbackPolicy(connect_host).is_literal_loopback is True

    def test_corrupt_default_db_config_returns_none(self) -> None:
        # Non-raising contract: a corrupt default-db config (resolve_db_paths
        # raises ValueError) must make the probe return None, not propagate a
        # raw ValueError — the resolution runs inside _serve_token's try.
        with patch("quarry.client.config.Settings") as mock_settings:
            mock_settings.active_db.return_value = "../evil"
            mock_settings.load.return_value.resolve_db_paths.side_effect = ValueError(
                "db name contains a path separator"
            )
            assert ClientConfig.loopback_token("127.0.0.1") is None

    def test_unreadable_default_db_config_returns_none(self) -> None:
        # active_db() raising OSError must also yield None, not propagate.
        with patch("quarry.client.config.Settings") as mock_settings:
            mock_settings.active_db.side_effect = OSError("config unreadable")
            assert ClientConfig.loopback_token("127.0.0.1") is None


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
            cfg = ClientConfig.from_login({"url": "wss://127.0.0.1:8420/mcp"})

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


class TestCanonicalUrl:
    """READ-path migration: a stored loopback NAME URL rewrites to the literal."""

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ("wss://localhost:8420/mcp", "wss://127.0.0.1:8420/mcp"),
            ("wss://LOCALHOST:8420/mcp", "wss://127.0.0.1:8420/mcp"),
            ("wss://localhost.:8420/mcp", "wss://127.0.0.1:8420/mcp"),
            ("ws://localhost:8420", "ws://127.0.0.1:8420"),
            ("wss://localhost/mcp", "wss://127.0.0.1/mcp"),  # no port
        ],
    )
    def test_loopback_name_migrates_to_literal(
        self, stored: str, expected: str
    ) -> None:
        assert ClientConfig.canonical_url(stored) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "wss://127.0.0.1:8420/mcp",  # already the literal
            "wss://[::1]:8420/mcp",  # already a literal loopback
            "wss://quarry.example.com:8420/mcp",  # (c) remote unaffected
            "wss://10.0.0.5:8420/mcp",  # remote IP unaffected
        ],
    )
    def test_literal_or_remote_url_unchanged(self, url: str) -> None:
        # (c) A genuine remote URL — and an already-literal loopback URL — is
        # returned byte-for-byte: no host rewrite, so the operator's exact
        # target and its stored bearer both stand.
        assert ClientConfig.canonical_url(url) == url

    def test_migrated_url_is_a_literal_loopback_target(self) -> None:
        # The migrated URL classifies as a live-token target, and the raw name
        # URL does too (because is_loopback_url migrates first) — but the token
        # is only ever presented over canonical_url, which is the literal.
        assert ClientConfig.is_loopback_url("wss://localhost:8420/mcp") is True
        migrated = ClientConfig.canonical_url("wss://localhost:8420/mcp")
        assert ClientConfig.is_loopback_url(migrated) is True
        assert "localhost" not in migrated

    def test_ipv6_bracketed_loopback_round_trips(self) -> None:
        # A stored bracketed IPv6 loopback URL is a literal-loopback target and
        # survives canonical_url unchanged (early return) — the brackets that
        # make it a valid URL must not be stripped or doubled.
        url = "wss://[::1]:8420/mcp"
        assert ClientConfig.canonical_url(url) == url
        assert ClientConfig._host_of(url) == "::1"
        assert ClientConfig.is_loopback_url(url) is True

    def test_ipv6_remote_url_unchanged(self) -> None:
        # A remote IPv6 URL is returned byte-for-byte and is not a live-token
        # target (its stored bearer stands).
        url = "wss://[2001:db8::5]:8420/mcp"
        assert ClientConfig.canonical_url(url) == url
        assert ClientConfig.is_loopback_url(url) is False

    def test_malformed_port_returns_url_unchanged(self) -> None:
        # Exception boundary: urlparse still yields hostname "localhost" for a
        # non-numeric port, so the migrate branch runs, but urlsplit(...).port
        # raises ValueError.  canonical_url must fail closed (return the URL
        # unchanged for the connection layer to reject), never crash with a raw
        # ValueError.
        url = "wss://localhost:bad/mcp"
        assert ClientConfig.canonical_url(url) == url  # no raise, unchanged

    def test_well_formed_url_still_canonicalizes_after_guard(self) -> None:
        # The fail-closed guard must not suppress the normal migration.
        assert (
            ClientConfig.canonical_url("wss://localhost:8420/mcp")
            == "wss://127.0.0.1:8420/mcp"
        )


class TestFromLoginRemoteUnaffected:
    def test_remote_url_not_canonicalized(self, tmp_path: Path) -> None:
        # (c) A remote login is unaffected by read-path migration: its URL is
        # unchanged and its stored bearer stands (no serve.token read).
        (tmp_path / "serve.token").write_text("local-token")
        with _run_dir_at(tmp_path):
            cfg = ClientConfig.from_login(
                {
                    "url": "wss://gpu.example.com:8420/mcp",
                    "headers": {"Authorization": "Bearer remote-key"},
                }
            )
        assert cfg.url == "wss://gpu.example.com:8420/mcp"
        resolved = cfg.token
        assert resolved == "remote-key"
        assert cfg.is_loopback is False
