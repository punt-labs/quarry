"""Resolve the single daemon target every client uses, in precedence order.

This is a distinct responsibility from :class:`ClientConfig` (which holds one
resolved target and reads the loopback credentials): :class:`TargetResolver`
picks *which* target to build — explicit env, a stored remote login, or the local
daemon — and connects a :class:`QuarryClient` to it.  There is no engine fallback:
a down local daemon fails closed with the autostart nudge.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import final

from quarry.client.client import QuarryClient
from quarry.client.config import ClientConfig, ClientConfigError
from quarry.client.errors import QuarryConnectionError
from quarry.net import LoopbackPolicy
from quarry.remote import read_proxy_config, ws_to_http

logger = logging.getLogger(__name__)

# The daemon's pinned CA (written by ``quarry install``); a managed daemon serves
# ``--tls`` with it, so a loopback client verifies against it when present and
# falls back to plaintext only for a bare ``quarryd`` with no CA on disk.
_DAEMON_CA_PATH = Path.home() / ".punt-labs" / "quarry" / "tls" / "ca.crt"

# The literal loopback the daemon binds and the client pins — never the ambiguous
# name ``localhost``, which a dual-stack resolver could point at a co-tenant.
_LOOPBACK_HOST = "127.0.0.1"


@final
class TargetResolver:
    """Resolve and connect to the one daemon target (env → login → loopback)."""

    __slots__ = ()

    @classmethod
    def connect(cls) -> QuarryClient:
        """Resolve the daemon target and return a connected :class:`QuarryClient`.

        The single construction seam every CLI data command shares: there is no
        local-vs-remote fork and no engine fallback — a command is unconditionally
        a client call against whatever :meth:`resolve` yields.
        """
        return QuarryClient.connect(cls.resolve())

    @classmethod
    def resolve(cls) -> ClientConfig:
        """Resolve the one daemon target, in precedence order (three tiers).

        1. explicit env (``QUARRY_URL`` + optional ``QUARRY_TOKEN``);
        2. a stored remote login (``quarry.toml`` with a ``url``);
        3. the local daemon on literal loopback (``serve.port`` + live
           ``serve.token`` from the run dir).

        Fail closed: tier 3 with no ``serve.port`` (the daemon is down or
        uninstalled) raises :class:`QuarryConnectionError` — never a silent
        engine fallback.
        """
        env_url = os.environ.get("QUARRY_URL")
        if env_url:
            return cls._from_env(env_url)
        login = cls._stored_login()
        if login is not None:
            return cls._from_stored_login(login)
        return cls._loopback_default()

    @classmethod
    def _from_stored_login(cls, login: Mapping[str, object]) -> ClientConfig:
        """Build from a stored login; surface a down loopback daemon uniformly.

        A stored literal-loopback login whose quarryd is down raises
        :class:`ClientConfigError` from the live serve.token read.  Re-raise it as
        the same typed :class:`QuarryConnectionError` tier 3 uses — daemon-down
        (no serve.port) vs token-unreadable (serve.port present) — so every
        loopback-down path guides the operator identically instead of one path
        leaking a raw RuntimeError.
        """
        try:
            return ClientConfig.from_login(login)
        except ClientConfigError as exc:
            if ClientConfig.is_loopback_url(str(login.get("url", ""))):
                raise cls._loopback_failure() from exc
            raise

    @classmethod
    def _loopback_failure(cls) -> QuarryConnectionError:
        """Return the typed loopback failure, token-vs-daemon-down aware.

        Shared by tier 2 (stored loopback login) and tier 3 (loopback default):
        serve.port absent → daemon down (autostart nudge); serve.port present but
        the token is unreadable/empty/stale → the token-specific guidance.
        """
        try:
            cls._loopback_port()
        except QuarryConnectionError as exc:
            return exc
        return QuarryConnectionError(cls._token_unreadable_message(), _LOOPBACK_HOST)

    @classmethod
    def _from_env(cls, url: str) -> ClientConfig:
        """Build a target from ``QUARRY_URL``/``QUARRY_TOKEN``/``QUARRY_CA_CERT``.

        Fail closed rather than transmit a bearer in cleartext to a host that is
        not same-machine: a passive eavesdropper on the wire would capture it.
        Plaintext to a loopback host is same-machine (like tier 3) and stays
        allowed.  The secure remote path is ``wss://`` + ``QUARRY_CA_CERT``.
        """
        # Strip as the daemon does, so a trailing newline from `$(cat key)` is
        # not presented verbatim and 401'd; whitespace-only ⇒ no bearer.
        token = (os.environ.get("QUARRY_TOKEN") or "").strip() or None
        # Migrate a loopback NAME (localhost) to the 127.0.0.1 literal BEFORE
        # gating and building, so a bearer is never presented to a name a
        # dual-stack resolver could redirect to a co-tenant's ::1 — mirrors
        # ClientConfig.from_login's canonicalization.
        url = ClientConfig.canonical_url(url)
        parsed = urllib.parse.urlparse(ws_to_http(url))
        scheme = parsed.scheme or "http"
        # Validate at the boundary: a QUARRY_URL with no host (ws://:9000,
        # ws:///path) must fail fast, not silently fall back to localhost — the
        # operator set a target and deserves an error, not a split-horizon default.
        if not parsed.hostname:
            raise ClientConfigError(f"QUARRY_URL has no host: {url!r}")
        host = parsed.hostname
        # Plaintext + bearer is allowed ONLY to a LITERAL loopback IP (same
        # machine); a name or a remote host must never receive the token in
        # cleartext.
        cleartext = scheme == "http" and not LoopbackPolicy(host).is_literal_loopback
        if token is not None and cleartext:
            raise ClientConfigError(
                "refusing to send QUARRY_TOKEN in cleartext to non-loopback "
                f"host {host!r}: use a wss:// URL with QUARRY_CA_CERT, or unset it."
            )
        return ClientConfig(url, cls._env_ca(scheme, host), token)

    @staticmethod
    def _env_ca(scheme: str, host: str) -> str | None:
        """Return the CA to pin for a TLS env target, else None.

        An explicit ``QUARRY_CA_CERT`` pins any TLS target — the sanctioned
        secure remote-env path.  Absent it, the LOCAL daemon CA is pinned only
        for a LITERAL loopback TLS target; it is the wrong CA for a remote host,
        so a remote ``wss://`` with no ``QUARRY_CA_CERT`` gets no pin and the
        transport fails closed rather than trusting the wrong CA.  None = a
        plaintext target, or a TLS target with no applicable CA (a documented
        transport state, not an unresolved value).
        """
        if scheme != "https":
            return None
        ca_env = (os.environ.get("QUARRY_CA_CERT") or "").strip()
        if ca_env:
            return ca_env
        if LoopbackPolicy(host).is_literal_loopback and _DAEMON_CA_PATH.exists():
            return str(_DAEMON_CA_PATH)
        return None

    @classmethod
    def _loopback_default(cls) -> ClientConfig:
        """Build the local-daemon target on literal loopback (tier 3)."""
        port = cls._loopback_port()
        # A managed daemon serves --tls with the pinned CA; only a bare quarryd
        # with no CA on disk is plaintext.  Detection mirrors doctor_daemon.
        tls = _DAEMON_CA_PATH.exists()
        scheme = "wss" if tls else "ws"
        url = f"{scheme}://{_LOOPBACK_HOST}:{port}"
        ca_cert = str(_DAEMON_CA_PATH) if tls else None
        # The live serve.token is presented ONLY to the literal loopback IP built
        # above — never a name a resolver could redirect to a co-tenant.  serve.port
        # was already read above, so the daemon is UP: a None token here means its
        # serve.token is unreadable/empty/stale (the multi-user case), a distinct
        # cause from "daemon down" — surface that, not the generic autostart nudge.
        token = ClientConfig.loopback_token(_LOOPBACK_HOST)
        if token is None:
            raise QuarryConnectionError(cls._token_unreadable_message(), _LOOPBACK_HOST)
        return ClientConfig(url, ca_cert, token)

    @classmethod
    def _loopback_port(cls) -> int:
        """Return the daemon's bound port, or fail closed with the autostart nudge."""
        try:
            return ClientConfig.active_run_dir().port_file.read()
        except (OSError, ValueError) as exc:
            raise QuarryConnectionError(cls._down_message(), _LOOPBACK_HOST) from exc

    @staticmethod
    def _down_message() -> str:
        """The fail-closed message when the local daemon is not running."""
        return (
            "quarryd is not running (no serve.port in the run dir). "
            "Start it with 'quarry install' (managed) or 'quarryd', then retry."
        )

    @staticmethod
    def _token_unreadable_message() -> str:
        """The fail-closed message when quarryd is up but its serve.token is bad."""
        return (
            "quarryd is running but its serve.token is unreadable or stale "
            "(another user may own it, or it is empty/corrupt). Run 'quarry doctor'."
        )

    @staticmethod
    def _stored_login() -> Mapping[str, object] | None:
        """Return a stored remote login with a ``url``, or None (tier 2 probe).

        None = no usable remote login: the config is absent, malformed
        (``ValueError``), unreadable (``OSError`` — permissions, transient IO), or
        has a ``url`` with no host.  None of these may crash the CLI (bug class 2),
        but silently ignoring the operator's remote config — or silently sending
        its stored bearer to a defaulted localhost — would be a split-horizon
        surprise, so warn before falling through to the loopback default.
        """
        try:
            config = read_proxy_config()
        except (ValueError, OSError) as exc:
            logger.warning(
                "Ignoring unreadable/malformed quarry.toml, using the local daemon: %s",
                exc,
            )
            return None
        quarry_cfg = config.get("quarry")
        url = quarry_cfg.get("url") if isinstance(quarry_cfg, Mapping) else None
        if not url:
            return None
        # Validate the host at the boundary: a stored url with no host would
        # default to localhost in httpx, silently redirecting the STORED BEARER to
        # a local target. Warn and fall back to the loopback default instead.
        if not urllib.parse.urlparse(ws_to_http(str(url))).hostname:
            logger.warning(
                "Ignoring quarry.toml with no host in url, using the local daemon: %s",
                url,
            )
            return None
        return quarry_cfg
