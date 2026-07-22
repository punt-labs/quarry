"""One real-loopback-TLS contract smoke test for the DES-031 remote wire.

A self-signed CA signs a server certificate carrying a ``127.0.0.1`` IP SAN; a
real ``uvicorn`` TLS server binds an ephemeral loopback port; and the SHIPPED
``QuarryClient`` — pinned to that CA (system roots excluded), presenting its
bearer over the encrypted channel — completes ``/health`` plus one ``/v1``
operation. That is the whole client↔daemon TLS contract exercised end-to-end
against real code, not a TestClient stand-in.

Tiering (quarry-5pg1): a live server over a real socket is CI-fragile in the
fast unit suite, so this ONE test is marked ``slow``. The fast job runs
``-m 'not slow'`` and never starts the server; it runs in the wheel gate / via
``-m slow``. Keep it to a single test — it is a wire smoke, not a route matrix.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

import pytest
import uvicorn

from quarry.client import QuarryClient
from quarry.client.transport import HttpxTransport
from quarry.tls import generate_ca, generate_server_cert
from tests.inproc_daemon import InProcessDaemon

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from fastapi import FastAPI

_STARTUP_TIMEOUT_S = 10.0
_SHUTDOWN_TIMEOUT_S = 10.0


@final
class _ThreadedTlsServer:
    """Run the daemon app under a real uvicorn TLS server on a background thread."""

    _server: uvicorn.Server
    _thread: threading.Thread

    def __new__(cls, app: FastAPI, *, certfile: str, keyfile: str) -> Self:
        self = super().__new__(cls)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,  # OS-assigned ephemeral loopback port
            log_level="warning",
            lifespan="off",
            ssl_certfile=certfile,
            ssl_keyfile=keyfile,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        return self

    @contextmanager
    def running(self) -> Generator[int]:
        """Start the server, yield the bound port, and shut it down on exit.

        The readiness wait is INSIDE the try, so a startup timeout still runs
        the finally — signalling the server to exit and joining the thread.
        Otherwise a raised timeout would leave the thread running and its bound
        socket held, leaking a live server into later tests (quarry-5pg1).
        """
        self._thread.start()
        try:
            self._await_started()
            yield self._bound_port()
        finally:
            self._server.should_exit = True
            self._thread.join(timeout=_SHUTDOWN_TIMEOUT_S)

    def _await_started(self) -> None:
        """Block until the server reports started, raising on the startup timeout."""
        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        while not self._server.started:
            if time.monotonic() > deadline:
                msg = "TLS server did not start within the startup timeout"
                raise RuntimeError(msg)
            time.sleep(0.05)

    def _bound_port(self) -> int:
        """Return the ephemeral port uvicorn bound after startup."""
        return int(self._server.servers[0].sockets[0].getsockname()[1])

    @property
    def is_thread_alive(self) -> bool:
        """Whether the server thread is still running (for leak assertions)."""
        return self._thread.is_alive()


@final
@dataclass(frozen=True, slots=True)
class _TlsFixture:
    """A configured (not-yet-started) TLS server plus the client's pinned CA."""

    server: _ThreadedTlsServer
    ca_cert: Path
    token: str


@pytest.fixture
def tls_fixture(tmp_path: Path) -> _TlsFixture:
    """Generate a pinned CA + loopback server cert and wire an in-process daemon."""
    ca_cert_pem, ca_key_pem = generate_ca()
    server_cert_pem, server_key_pem = generate_server_cert(
        ca_cert_pem, ca_key_pem, "127.0.0.1"
    )
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    ca_path = tls_dir / "ca.crt"
    ca_path.write_bytes(ca_cert_pem)
    crt_path = tls_dir / "server.crt"
    crt_path.write_bytes(server_cert_pem)
    key_path = tls_dir / "server.key"
    key_path.write_bytes(server_key_pem)

    auth = "tls-smoke-token"
    daemon = InProcessDaemon(tmp_path / "data", api_key=auth)
    server = _ThreadedTlsServer(
        daemon.app, certfile=str(crt_path), keyfile=str(key_path)
    )
    return _TlsFixture(server=server, ca_cert=ca_path, token=auth)


@pytest.mark.slow
def test_tls_loopback_client_round_trip(tls_fixture: _TlsFixture) -> None:
    """A real loopback quarryd over TLS answers a pinned-CA ``QuarryClient``.

    Asserts the full wire: the pinned-CA HTTPS handshake (the server cert's
    ``127.0.0.1`` IP SAN verifies, system roots excluded), the bearer over the
    encrypted channel, and both the unversioned ``/health`` and a ``/v1`` route
    returning the expected empty state on a fresh database.
    """
    with tls_fixture.server.running() as port:
        transport = HttpxTransport.from_mapping(
            {
                "url": f"https://127.0.0.1:{port}",
                "ca_cert": str(tls_fixture.ca_cert),
                "headers": {"Authorization": f"Bearer {tls_fixture.token}"},
            }
        )
        with transport:
            client = QuarryClient(transport)
            health = client.health()
            assert health.status == "ok"
            collections = client.list_collections()
            assert collections.total_collections == 0


@pytest.mark.slow
def test_tls_server_releases_thread_on_startup_timeout(
    tls_fixture: _TlsFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A startup timeout shuts the server down — no leaked thread or bound port.

    Force the readiness deadline into the past so ``_await_started`` raises on its
    first check (before uvicorn can bind). The raised ``RuntimeError`` must still
    run ``running()``'s finally, which signals ``should_exit`` and joins the
    thread — so no live server is left holding a socket (quarry-5pg1). A negative
    timeout makes the deadline already-elapsed, killing any start-vs-timeout race.
    """
    monkeypatch.setattr("tests.test_tls_smoke._STARTUP_TIMEOUT_S", -1.0)
    server = tls_fixture.server
    with pytest.raises(RuntimeError, match="did not start"), server.running():
        pass  # unreachable — the readiness wait times out first
    assert not server.is_thread_alive, "server thread leaked past a startup timeout"
