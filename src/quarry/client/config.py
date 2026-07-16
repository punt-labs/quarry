"""Resolve a client's daemon target: URL, pinned CA, and loopback bearer.

The daemon now requires ``serve.token`` on every request, including loopback
(DES-031 v2.2 R4).  A client logged in to a loopback URL must therefore present
that token — and because the daemon mints a fresh token on every restart, the
token is read *live* from the run dir, never trusted from the stored login
config (which would go stale the moment the supervisor respawns the daemon).

Presentation is gated on a LITERAL loopback IP, never a name: the client hands
the live serve.token only to a literal loopback address (127.0.0.0/8, ::1), so a
resolver cannot redirect the secret to a co-tenant's ``::1`` behind an ambiguous
``localhost``.  ``login`` canonicalizes ``localhost`` to ``127.0.0.1`` at WRITE
time (:meth:`canonical_host`); the READ path (:meth:`canonical_url`) repeats the
same migration so a config stored *before* the literal-IP flip — every prior
``wss://localhost:8420`` install — auto-migrates to ``wss://127.0.0.1:8420`` on
read.  That both presents the live token AND connects to the un-hijackable
literal, closing the lockout without reopening the exfiltration: the connection
targets ``127.0.0.1``, never the name a resolver could redirect.

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
from quarry.remote import to_netloc, ws_to_http
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
        """Whether the target is an eligible live-token target: a LITERAL loopback IP.

        Client-side "loopback" means a literal loopback address: the secret
        serve.token is presented only to an IP a resolver cannot redirect to a
        co-tenant.  A stored loopback NAME first migrates to the literal
        (:meth:`_target_host`), so a ``wss://localhost`` config counts — its
        connection is rewritten to ``127.0.0.1`` in lockstep (:meth:`from_login`).
        """
        return LoopbackPolicy(self._target_host(self._url)).is_literal_loopback

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

        The stored URL is first migrated (:meth:`canonical_url`): a loopback
        NAME like ``localhost`` becomes the ``127.0.0.1`` literal.  This runs
        BEFORE both the token gate and the stored URL, so a config written by an
        older client (``wss://localhost:8420``) resolves the LIVE ``serve.token``
        AND connects to the un-hijackable literal — never the ambiguous name a
        resolver could redirect to a co-tenant's ``::1``.  For a migrated literal
        loopback URL the bearer is read live from ``serve.token`` (it rotates
        each daemon restart); a genuine remote URL is left intact and keeps its
        stored bearer verbatim.
        """
        url = cls.canonical_url(str(login["url"]))
        raw_ca = login.get("ca_cert")
        ca_cert = str(raw_ca) if raw_ca else None
        token: str | None
        # ``url`` is already migrated, so a stored ``localhost`` presents here as
        # the literal and the stored URL below is the literal too — the gate and
        # the connection target can never diverge.
        if LoopbackPolicy(cls._host_of(url)).is_literal_loopback:
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
        if not LoopbackPolicy(host).is_literal_loopback:
            return None
        try:
            return cls._serve_token()
        except ClientConfigError:
            return None

    @classmethod
    def loopback_token_for_url(cls, url: str) -> str | None:
        """Return the live serve.token for a loopback ``ws(s)/http(s)`` URL.

        A stored loopback NAME migrates to the literal first (:meth:`_target_host`)
        so a ``wss://localhost`` URL is a live-token target — but the CALLER MUST
        connect to :meth:`canonical_url` of the same URL, or the live token would
        be presented over a connection to the ambiguous name a resolver controls.
        """
        return cls.loopback_token(cls._target_host(url))

    @classmethod
    def is_loopback_url(cls, url: str) -> bool:
        """Whether a URL is an eligible live-token target: a LITERAL loopback IP.

        Lets a caller distinguish a live-token target (present the LIVE
        serve.token) from any other (stored bearer) --- ``loopback_token_for_url``
        alone cannot, since it returns None for both a remote URL and a
        literal-loopback URL whose token is missing.  A stored loopback NAME URL
        migrates to the literal first (:meth:`_target_host`), so ``wss://localhost``
        IS eligible --- but a caller that presents the token on the strength of
        this MUST connect to :meth:`canonical_url` of the URL, never the raw name.
        """
        return LoopbackPolicy(cls._target_host(url)).is_literal_loopback

    @classmethod
    def is_loopback_host(cls, host: str) -> bool:
        """Whether a bare host is an eligible live-token target: a LITERAL loopback IP.

        A NAME (``localhost``) returns False: the secret serve.token is presented
        only to a literal loopback address a resolver cannot redirect.
        """
        return LoopbackPolicy(host).is_literal_loopback

    @classmethod
    def canonical_url(cls, url: str) -> str:
        """Migrate a stored URL's loopback NAME host to the IPv4 literal.

        A config stored before write-time canonicalization (``wss://localhost:8420``)
        must migrate at READ time to ``wss://127.0.0.1:8420``: the client both
        presents the live serve.token AND connects to the un-hijackable literal.
        The ambiguous name a resolver could point at a co-tenant's ``::1`` is
        never the connect target.  A literal-loopback or remote URL is returned
        unchanged (its host, port, path, and stored bearer all stand).
        """
        host = cls._host_of(url)
        policy = LoopbackPolicy(host)
        # Only a loopback NAME migrates.  A literal loopback is already
        # un-hijackable; a remote URL is left byte-for-byte, since the operator's
        # exact target (and its stored bearer) is not ours to rewrite.
        if policy.is_literal_loopback or not policy.is_loopback:
            return url
        # ``urlparse`` yields a hostname for a URL whose PORT is malformed
        # (``wss://localhost:bad``), so the migrate branch runs but
        # ``urlsplit(...).port`` raises ValueError.  Fail closed: return the URL
        # unchanged and let the connection layer reject it, rather than crash the
        # CLI path with a raw ValueError far from its cause.
        try:
            split = urllib.parse.urlsplit(url)
            # to_netloc brackets an IPv6 literal (and preserves an absent port) so
            # the reassembled URL always parses back to the same host.
            netloc = to_netloc(policy.canonical_host, split.port)
        except ValueError:
            return url
        return urllib.parse.urlunsplit(
            (split.scheme, netloc, split.path, split.query, split.fragment)
        )

    @classmethod
    def _target_host(cls, url: str) -> str:
        """The literal host a live serve.token would be presented to.

        A stored loopback NAME is migrated to the IPv4 literal, so the token gate
        (:meth:`is_loopback_url`, :attr:`is_loopback`) and the eventual
        connection (:meth:`canonical_url`) agree on the un-hijackable target.  A
        remote host is returned normalized and stays non-loopback.
        """
        return LoopbackPolicy(cls._host_of(url)).canonical_host

    @staticmethod
    def canonical_host(host: str) -> str:
        """Canonicalize a loopback NAME to the IPv4 loopback literal for storage.

        ``quarry login localhost`` stores ``127.0.0.1`` so the managed path
        presents the serve.token to the exact literal the daemon binds --- a
        name is never stored (a resolver could later point it at a co-tenant).
        Every other host is returned NORMALIZED (stripped, lowercased,
        de-trailing-dotted) by :attr:`LoopbackPolicy.canonical_host`, not
        byte-for-byte unchanged --- so ``login " 127.0.0.1 "`` cannot store an
        invalid whitespaced host in the URL.
        """
        return LoopbackPolicy(host).canonical_host

    @staticmethod
    def _serve_token() -> str:
        """Read the daemon's live loopback bearer, or raise if it is down.

        Fail closed: a missing ``serve.token`` means no daemon owns the run
        dir, so raise a clear error rather than return an empty bearer that
        would be rejected far from its cause.
        """
        # serve.token lives under the daemon's startup-db run dir; resolve the
        # ACTIVE database (the CLI's --db override, else the default) so a
        # loopback client against a --db daemon reads the matching token, not
        # the hardcoded default database's.  The resolution runs INSIDE the try:
        # active_db() can raise OSError on an unreadable default-db config, and
        # resolve_db_paths() raises ValueError on a default-db name containing a
        # path separator.  Both must surface as ClientConfigError so loopback_token
        # keeps its non-raising contract (returns None) rather than leaking a raw
        # OSError/ValueError to a caller expecting a fail-closed probe.
        try:
            settings = Settings.load().resolve_db_paths(Settings.active_db() or None)
            token = RunDir(settings.lancedb_path.parent).token_file.read()
        except (OSError, ValueError) as exc:
            # OSError (not just FileNotFoundError): a PermissionError on the 0600
            # token — e.g. another UID owns the run dir — must surface an
            # actionable error.  ValueError: a corrupt default-db config (a
            # path-separator name) must not escape as a raw crash.  Do not assert a
            # single cause: the daemon may be down, up with an unreadable token, or
            # the default-db config may be corrupt.
            msg = (
                "serve.token could not be read — quarryd is not running, or "
                "its serve.token is unreadable by this user. Run 'quarry doctor'."
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
        if not isinstance(auth, str):
            return None
        # Mirror the daemon's parser (``reject_unauthorized`` in
        # daemon/routes/base.py): split on ANY whitespace and require EXACTLY
        # two parts (scheme, token).  ``partition(" ")`` diverged on the two
        # cases the daemon rejects — a tab-separated header (``partition`` left
        # the scheme glued to the token, dropping a valid credential) and a
        # token containing whitespace (the daemon 401s a >2-part header, so the
        # client must not present one).  Scheme compared case-insensitively: a
        # stored "bearer <tok>" must resolve, not be sent tokenless (401).
        parts = auth.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        # ``split()`` never yields empty parts, so ``parts[1]`` is a non-empty
        # token — a bare "Bearer " (empty credential) fails the length check and
        # returns None, emitting NO Authorization header rather than a 401 one.
        return parts[1]

    @staticmethod
    def _host_of(url: str) -> str:
        """Return the hostname of a ``ws(s)://`` or ``http(s)://`` URL."""
        return urllib.parse.urlparse(ws_to_http(url)).hostname or ""
