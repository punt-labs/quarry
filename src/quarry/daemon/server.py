"""The daemon process: warm the engine, then serve over uvicorn.

The daemon is started via the ``quarryd`` entry point / :class:`DaemonLauncher`,
which bundles the bind options into a :class:`ServeConfig` and drives a
:class:`DaemonServer`.  The server warms the engine single-threaded (DES-032)
before accepting traffic, then --- only after a successful bind --- writes both
sidecars beside the data dir: ``serve.port`` (so ephemeral-port ``port=0``
callers can discover the bound port) and ``serve.token`` (the loopback bearer
clients present to authenticate).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from types import ModuleType
from typing import TYPE_CHECKING, Self, cast, final

import uvicorn

from quarry.config import DEFAULT_PORT, Settings
from quarry.daemon.app import build_app
from quarry.daemon.context import DaemonContext
from quarry.fd_telemetry import FdTelemetry
from quarry.remote import to_netloc
from quarry.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from socket import socket

    from starlette.applications import Starlette

logger = logging.getLogger(__name__)

# ``fcntl`` is POSIX-only and absent on non-POSIX platforms (e.g. Windows).
# Import it optionally so ``import quarry.daemon.server`` (and the ``quarryd``
# console script) never crashes at import there for callers that never start the
# daemon.  ``None`` = platform without fcntl; a daemon start on such a platform
# fails closed with a clear message because the run-dir lock — the daemon's
# exclusive-ownership guarantee — cannot be provided without it.
fcntl: ModuleType | None
try:
    fcntl = importlib.import_module("fcntl")
except ImportError:  # pragma: no cover - POSIX-only; non-POSIX import must not fail
    fcntl = None

# Sample the daemon's open-fd count every 5 minutes so a climbing trend — the
# LanceDB deleted-index-handle leak — surfaces in logs before it reaches EMFILE.
_FD_TELEMETRY_INTERVAL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class ServeConfig:
    """The daemon's bind options: address, auth, CORS, and TLS material."""

    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    api_key: str | None = None
    cors_origins: frozenset[str] | None = None
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None

    @property
    def tls_enabled(self) -> bool:
        return self.ssl_certfile is not None and self.ssl_keyfile is not None

    @property
    def scheme(self) -> str:
        return "https" if self.tls_enabled else "http"


@final
class DaemonServer:
    """Warm the engine and serve the REST API until a shutdown signal."""

    _settings: Settings
    _config: ServeConfig
    _run_dir: RunDir
    _bound: bool
    _lock_fd: int

    def __new__(cls, settings: Settings, config: ServeConfig) -> Self:
        config = cls._authenticated(config)
        self = super().__new__(cls)
        self._settings = settings
        self._config = config
        self._run_dir = RunDir(settings.lancedb_path.parent)
        self._bound = False
        self._lock_fd = -1  # -1 = lock not held; a real fd once acquired.
        return self

    @staticmethod
    def _authenticated(config: ServeConfig) -> ServeConfig:
        """Normalize the API key and refuse to serve without one (R4).

        Two guarantees at the single daemon boundary every caller passes through:

        - Strip the key once so ``serve.token``, the auth comparison, and the
          loopback client all use the same value.  An operator key with a
          trailing newline (``QUARRY_API_KEY=$(cat keyfile)``) would otherwise
          authenticate the raw value while the loopback client presents the
          stripped ``serve.token`` --- a 401 on the operator's own machine.
        - Fail closed: EVERY bind, loopback included, must carry a key (R4:
          loopback => token-required).  The ``quarryd`` launcher mints a
          loopback token, but a caller who passes none is refused here rather
          than run open --- auth can never silently disable, regardless of
          caller.  A non-loopback bind with no operator key is already refused
          earlier by the launcher (before a token is minted), so an
          auto-minted token can never satisfy a network bind.
        """
        key = (config.api_key or "").strip() or None
        if key is None:
            msg = (
                "Refusing to serve without an API key. Every bind must be "
                "authenticated (the quarryd launcher mints a loopback token)."
            )
            raise SystemExit(msg)
        return replace(config, api_key=key)

    @classmethod
    def serve(cls, settings: Settings, config: ServeConfig) -> None:
        """Warm the engine and serve *config* until shutdown (process entry).

        The bind options arrive already bundled in a :class:`ServeConfig`, so
        the caller (the ``quarryd`` entry point) owns option parsing and this
        method stays a two-argument seam rather than a wide parameter list.
        """
        cls(settings, config).run()

    def run(self) -> None:
        """Build the app, bind uvicorn, and block until shutdown.

        Acquire the run-dir lock FIRST so a second daemon on the same database
        fails closed here — before warming the engine or writing any shared
        sidecar — and release it on every exit path in the ``finally``.
        """
        self._acquire_run_dir_lock()
        try:
            ctx = DaemonContext(
                self._settings,
                api_key=self._config.api_key,
                cors_origins=self._config.cors_origins,
            )
            ctx.warm()  # Build cached resources single-threaded before serving.

            app = build_app(ctx, lifespan=self._lifespan)
            server = uvicorn.Server(self._uvicorn_config(app))
            self._install_startup_hook(server)
            logger.info(
                "Starting Quarry server on %s:%d",
                self._config.host,
                self._config.port,
            )
            server.run()
            logger.info("Server stopped")
        finally:
            self._release_run_dir_lock()

    def _acquire_run_dir_lock(self) -> None:
        """Take an exclusive advisory lock so ONE daemon owns this run dir.

        serve.token / serve.port are shared per-database sidecars.  A SECOND
        quarryd on the same database — even on a different port, so the bind
        never conflicts — would overwrite the first's serve.token (the first's
        clients then 401) and, on its own exit, delete the first's sidecars
        (locking everyone out).  An exclusive ``flock`` held for the process
        lifetime makes the lock-holder the ONLY writer: it closes that clobber
        AND the token-writer's temp-retry race (no concurrent writer can exist).
        Fails closed: a second daemon exits rather than corrupt the shared token.
        """
        # fcntl is POSIX-only: on a platform without it the run-dir lock cannot
        # be provided, so fail closed at daemon start (never a silent no-op that
        # would reopen the clobber race) — and never at import (see the guard).
        if fcntl is None:
            msg = "quarryd requires a POSIX platform (fcntl is unavailable)."
            raise SystemExit(msg)
        lock_path = self._run_dir.lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        # Close the fd on EVERY flock failure — contention OR any other OSError
        # (permission, filesystem) — so a failed acquire never leaks a
        # descriptor (this daemon has EMFILE history).  _lock_fd is set only
        # AFTER the lock is held, so the release path never touches an fd whose
        # flock failed.
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            msg = (
                "another quarryd already owns this database's run dir "
                f"({lock_path.parent})"
            )
            raise SystemExit(msg) from exc
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd

    def _release_run_dir_lock(self) -> None:
        """Release the run-dir lock and close its fd (idempotent).

        Close the fd in a ``finally`` so a failing ``LOCK_UN`` (EINTR / OSError)
        still frees the descriptor — release must never leak, mirroring the
        acquire-side close-on-all-paths.  ``_lock_fd`` is cleared first so a
        second call is a no-op.  ``flock`` also releases when the fd closes on
        process exit, so a crashed daemon frees the lock for the next start.
        """
        if self._lock_fd < 0:
            return
        fd = self._lock_fd
        self._lock_fd = -1
        try:
            # fcntl held the lock, so it was available at acquire; guard anyway
            # to satisfy the optional-import type and never deref None.
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @asynccontextmanager
    async def _lifespan(self, _app: Starlette) -> AsyncGenerator[None]:
        # Runs for the daemon's lifetime; cancelled on shutdown below.
        monitor = asyncio.create_task(FdTelemetry(_FD_TELEMETRY_INTERVAL_SECONDS).run())
        try:
            yield
        finally:
            monitor.cancel()
            # Remove ONLY the sidecars this instance wrote after its own
            # successful bind.  A second quarryd that fails to bind (port in
            # use) must not delete a running peer's live serve.token / port
            # file on the shared path on its way out.
            if self._bound:
                self._run_dir.port_file.remove()
                self._run_dir.token_file.remove()
            with suppress(asyncio.CancelledError):
                await monitor

    def _uvicorn_config(self, app: Starlette) -> uvicorn.Config:
        return uvicorn.Config(
            app,
            host=self._config.host,
            port=self._config.port,
            log_config=None,
            log_level="warning",
            access_log=False,
            ssl_certfile=self._config.ssl_certfile,
            ssl_keyfile=self._config.ssl_keyfile,
        )

    def _install_startup_hook(self, server: uvicorn.Server) -> None:
        """Wrap uvicorn startup to write the sidecars AFTER a successful bind.

        Both ``serve.port`` (which also lets ``port=0`` callers discover the
        OS-assigned port) and ``serve.token`` are written only once the
        socket has bound.  A failed bind therefore never writes them, so a
        second instance racing an already-bound port cannot clobber a running
        peer's live token.  The write lands microseconds after the serve loop
        starts accepting; a client that races that window fails closed and
        retries --- an acceptable trade for not nuking a live peer.
        """
        original_startup = server.startup

        async def _startup_with_sidecars(sockets: list[socket] | None = None) -> None:
            await original_startup(sockets=sockets)
            if server.servers and server.servers[0].sockets:
                actual_port = server.servers[0].sockets[0].getsockname()[1]
                self._write_sidecars(actual_port)
                logger.info(
                    "Quarry server listening on %s://%s",
                    self._config.scheme,
                    # Bracket an IPv6 literal for the log so it renders a valid
                    # URL (https://[::1]:8420, not the ambiguous ::1:8420); the
                    # bind host is unchanged.
                    to_netloc(self._config.host, actual_port),
                )
            else:
                logger.error(
                    "Server started but no bound sockets; sidecars not written"
                )

        server.startup = _startup_with_sidecars  # type: ignore[method-assign]

    def _write_sidecars(self, actual_port: int) -> None:
        """Write serve.token + serve.port all-or-nothing after a successful bind.

        On any failure — an OSError OR an interrupt (KeyboardInterrupt / SIGINT)
        landing between the two writes — remove ONLY the sidecars THIS instance
        wrote, never a peer's on the shared per-db path, then re-raise.  Catching
        BaseException matters: a SIGINT after serve.token but before serve.port
        would otherwise leave a lone serve.token that loopback clients read as a
        false "daemon up".  Both writes are atomic (a failed write leaves
        nothing), so the flags record only completed writes.  ``_bound`` is set
        only after both succeed, keeping the shutdown cleanup consistent.
        """
        wrote_token = False
        wrote_port = False
        try:
            # _authenticated guarantees a non-empty key on every instance, so the
            # token is always written; the cast records that boundary invariant.
            self._run_dir.token_file.write(cast("str", self._config.api_key))
            wrote_token = True
            self._run_dir.port_file.write(actual_port)
            wrote_port = True
        except BaseException:
            if wrote_token:
                self._run_dir.token_file.remove()
            if wrote_port:
                self._run_dir.port_file.remove()
            raise
        self._bound = True
