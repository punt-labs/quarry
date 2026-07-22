"""In-process daemon for hermetic daemon-mandatory tests (DES-031).

Drives the REAL daemon FastAPI app through ``httpx.ASGITransport`` — no socket,
no ONNX model — so a CLI/MCP test that would otherwise run ``resolve()`` and hit
a LIVE ``quarryd`` instead exercises the real request handlers in-process. Patch
``TargetResolver.connect`` to return the client from :meth:`InProcessDaemon.client`
and the whole ``CLI → QuarryClient → daemon → LanceDB`` round-trip runs against
real code on a real (empty) tmp database and passes with NO daemon running — the
"daemon-mandatory tests must be hermetic" rule, enforceable with the daemon
STOPPED.

The transport is a genuine ``httpx`` client over ``httpx.ASGITransport``: it
returns a real ``httpx.Response`` that the production ``HttpxTransport._parse``
consumes unchanged, so the hermetic path matches the shipped client's wire
behaviour byte-for-byte (bug-class-3). ``QuarryClient`` is synchronous, so an
``anyio`` blocking portal bridges its calls onto the async ASGI client.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
from typing import TYPE_CHECKING, Self, final

import anyio.from_thread
import httpx
import numpy as np

from quarry.client import QuarryClient
from quarry.client.transport import HttpxTransport, Response
from quarry.config import Settings
from quarry.daemon.app import build_app
from quarry.daemon.context import DaemonContext

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping
    from pathlib import Path

    from anyio.from_thread import BlockingPortal
    from fastapi import FastAPI
    from numpy.typing import NDArray


@final
class FakeEmbedder:
    """A deterministic zero-vector embedder so the daemon skips the ONNX model.

    Satisfies ``quarry.types.EmbeddingBackend`` structurally; every vector is
    zeros of the configured dimension, which is all a hermetic route test needs
    (search over an empty table returns nothing regardless of the query vector).
    """

    _dimension: int

    def __new__(cls, dimension: int = 768) -> Self:
        self = super().__new__(cls)
        self._dimension = dimension
        return self

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return "fake-embedder"

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        return np.zeros((len(texts), self._dimension), dtype=np.float32)

    def embed_query(self, query: str) -> NDArray[np.float32]:
        return np.zeros(self._dimension, dtype=np.float32)


@final
class AsgiTransport:
    """A ``QuarryClient`` transport over ``httpx.ASGITransport`` (no socket).

    Holds an async ``httpx`` client bound to the daemon's ASGI app and a blocking
    portal that runs each request on the portal's event loop, so a synchronous
    ``QuarryClient`` call reaches the async client. The genuine ``httpx.Response``
    it produces is handed to the production ``HttpxTransport._parse`` unchanged,
    so the hermetic path cannot drift from the shipped wire behaviour.
    """

    _client: httpx.AsyncClient
    _portal: BlockingPortal

    def __new__(cls, client: httpx.AsyncClient, portal: BlockingPortal) -> Self:
        self = super().__new__(cls)
        self._client = client
        self._portal = portal
        return self

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        # timeout is irrelevant in-process (no socket); accepted for protocol parity.
        timeout: float | None = None,
    ) -> Response:
        """Run the request on the portal loop and parse it as production does."""
        resp = self._portal.call(
            functools.partial(
                self._client.request,
                method,
                path,
                params=dict(params) if params else None,
                json=dict(json_body) if json_body is not None else None,
            )
        )
        # Reuse the shipped parser so the hermetic path matches the wire exactly.
        return HttpxTransport._parse(resp)


@final
class InProcessDaemon:
    """The real daemon app wired for hermetic tests: real handlers, tmp DB, no ONNX."""

    _ctx: DaemonContext
    _app: FastAPI

    def __new__(cls, data_dir: Path, *, api_key: str | None = None) -> Self:
        self = super().__new__(cls)
        (data_dir / "lancedb").mkdir(parents=True, exist_ok=True)
        settings = Settings(
            lancedb_path=data_dir / "lancedb",
            registry_path=data_dir / "registry.db",
        )
        # With api_key set the routes enforce bearer auth — the TLS smoke uses it
        # to exercise the authenticated wire; None (default) leaves routes open.
        # The fake embedder goes through DaemonContext's public DI seam, so no
        # ONNX session is ever built and no private resource internals are touched.
        ctx = DaemonContext(
            settings,
            api_key=api_key,
            embedder=FakeEmbedder(settings.embedding_dimension),
        )
        self._ctx = ctx
        self._app = build_app(ctx)
        return self

    @property
    def app(self) -> FastAPI:
        """The daemon's ASGI application (drive it via :meth:`client`)."""
        return self._app

    @property
    def ctx(self) -> DaemonContext:
        """The daemon context, for tests that inspect tasks or the ingest queue."""
        return self._ctx

    @contextmanager
    def client(self) -> Generator[QuarryClient]:
        """Yield a ``QuarryClient`` driving this app over ``httpx.ASGITransport``.

        A blocking portal owns the event loop for the async client's lifetime and
        closes it on exit, so no loop or connection leaks past the ``with`` block.
        """
        transport = httpx.ASGITransport(app=self._app, raise_app_exceptions=False)
        http_client = httpx.AsyncClient(transport=transport, base_url="http://daemon")
        with anyio.from_thread.start_blocking_portal() as portal:
            try:
                yield QuarryClient(AsgiTransport(http_client, portal))
            finally:
                portal.call(http_client.aclose)
