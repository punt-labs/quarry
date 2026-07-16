"""Resolve a client's daemon target: URL, pinned CA, and loopback bearer.

The daemon now requires ``serve.token`` on every request, including loopback
(DES-031 v2.2 R4).  A client logged in to a loopback URL must therefore present
that token — and because the daemon mints a fresh token on every restart, the
token is read *live* from the run dir, never trusted from the stored login
config (which would go stale the moment the supervisor respawns the daemon).

Fail closed: a loopback target whose ``serve.token`` is unreadable raises rather
than returning a tokenless config, so a down daemon surfaces a clear error
instead of a bare 401 far from its cause.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from typing import Self, final

from quarry.config import Settings
from quarry.net import LoopbackPolicy
from quarry.remote import ws_to_http
from quarry.remote_client import RemoteClient
from quarry.run_dir import RunDir


@final
class ClientConfigError(RuntimeError):
    """A daemon target could not be resolved (e.g. loopback token missing)."""


@final
class ClientConfig:
    """One resolved daemon target: URL, optional pinned CA, and bearer token."""

    _url: str
    _ca_cert: str | None
    _token: str | None

    def __new__(cls, url: str, ca_cert: str | None, token: str | None) -> Self:
        self = super().__new__(cls)
        self._url = url
        self._ca_cert = ca_cert
        self._token = token
        return self

    @property
    def url(self) -> str:
        return self._url

    @property
    def ca_cert(self) -> str | None:
        # None = plaintext target (no pinned CA); a documented transport state,
        # not a failure to resolve one.
        return self._ca_cert

    @property
    def token(self) -> str | None:
        # None = an unauthenticated remote server (the operator logged in with
        # no bearer); a documented auth state, not an unresolved value.
        return self._token

    @property
    def is_loopback(self) -> bool:
        """Return whether the target URL names only this host."""
        return LoopbackPolicy(self._host_of(self._url)).is_loopback

    def remote_mapping(self) -> dict[str, object]:
        """Return the ``{url, ca_cert?, headers?}`` config a REST client consumes.

        The token is rendered as an ``Authorization: Bearer`` header only when
        present, so an unauthenticated remote server carries no header.
        """
        mapping: dict[str, object] = {"url": self._url}
        if self._ca_cert is not None:
            mapping["ca_cert"] = self._ca_cert
        if self._token is not None:
            mapping["headers"] = {"Authorization": f"Bearer {self._token}"}
        return mapping

    @classmethod
    def from_login(cls, login: Mapping[str, object]) -> Self:
        """Build a target from a stored login config, resolving the bearer.

        For a loopback URL the bearer is read live from ``serve.token`` (it
        rotates each daemon restart); for a genuine remote URL the stored
        bearer is used verbatim (the server operator set a stable key).
        """
        url = str(login["url"])
        raw_ca = login.get("ca_cert")
        ca_cert = str(raw_ca) if raw_ca else None
        token: str | None
        if LoopbackPolicy(cls._host_of(url)).is_loopback:
            token = cls._serve_token()
        else:
            token = cls._login_bearer(login)
        return cls(url, ca_cert, token)

    @classmethod
    def remote_client(cls, login: Mapping[str, object]) -> RemoteClient:
        """Build a RemoteClient for a login config, injecting the loopback token.

        The construction seam every CLI remote path shares: resolve the login
        (loopback ⇒ live serve.token, remote ⇒ stored bearer) and wrap it in
        the transport.  Living in the client tier keeps each CLI call site a
        bare swap; RemoteClient is superseded by QuarryClient in PR-3.
        """
        return RemoteClient(cls.from_login(login).remote_mapping())

    @classmethod
    def loopback_token(cls, host: str) -> str | None:
        """Return the live serve.token for a loopback host, else None.

        Non-raising: the probe paths (login validation, ``remote --ping``)
        must present the live loopback bearer when the daemon is up, but a
        missing token means the daemon is down --- those paths report
        "unreachable" from the connection itself, so this returns None rather
        than fail closed. The construction path uses ``from_login``, which
        DOES fail closed. Returns None for a non-loopback host (its stored
        bearer stands).
        """
        if not LoopbackPolicy(host).is_loopback:
            return None
        try:
            return cls._serve_token()
        except ClientConfigError:
            return None

    @classmethod
    def loopback_token_for_url(cls, url: str) -> str | None:
        """Return the live serve.token for a loopback ``ws(s)/http(s)`` URL."""
        return cls.loopback_token(cls._host_of(url))

    @staticmethod
    def _serve_token() -> str:
        """Read the daemon's live loopback bearer, or raise if it is down.

        Fail closed: a missing ``serve.token`` means no daemon owns the run
        dir, so raise a clear error rather than return an empty bearer that
        would be rejected far from its cause.
        """
        settings = Settings.load().resolve_db_paths(Settings.read_default_db() or None)
        try:
            token = RunDir(settings.lancedb_path.parent).token_file.read()
        except OSError as exc:
            # OSError (not just FileNotFoundError): a PermissionError on the
            # 0600 token — e.g. another UID owns the run dir — must surface the
            # same actionable error, never a raw OSError from deep in the call.
            msg = (
                "Loopback daemon requires a token but serve.token could not be "
                "read — quarryd is not running. Run 'quarry doctor'."
            )
            raise ClientConfigError(msg) from exc
        # An empty/whitespace token is a corrupt file, not a credential:
        # fail closed rather than emit an empty ``Authorization: Bearer``.
        # Unreachable via the atomic writer today, but make the "never an
        # empty bearer" invariant structural, not just incidental.
        if not token:
            raise ClientConfigError(
                "serve.token is empty — quarryd wrote a corrupt token file. "
                "Run 'quarry doctor'."
            )
        return token

    @staticmethod
    def _login_bearer(login: Mapping[str, object]) -> str | None:
        """Return the stored ``Bearer`` token from a login config, or None.

        None = an unauthenticated remote server; a documented state, not a
        failure to produce a value.
        """
        headers = login.get("headers")
        if not isinstance(headers, Mapping):
            return None
        auth = headers.get("Authorization")
        if not isinstance(auth, str) or not auth.startswith("Bearer "):
            return None
        return auth.removeprefix("Bearer ")

    @staticmethod
    def _host_of(url: str) -> str:
        """Return the hostname of a ``ws(s)://`` or ``http(s)://`` URL."""
        return urllib.parse.urlparse(ws_to_http(url)).hostname or ""
