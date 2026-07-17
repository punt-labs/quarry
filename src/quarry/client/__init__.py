"""Client-tier package: resolve a daemon target and its loopback bearer.

Layer 2 of DES-031 v2.2 — imports only ``quarry.api``/shared primitives, never
the engine.  Holds :class:`ClientConfig`, which turns a stored login config into
the URL + pinned CA + bearer a client presents, reading the live ``serve.token``
for loopback targets so the rotating daemon credential is never stale.
"""

from __future__ import annotations

from quarry.client.config import ClientConfig, ClientConfigError

__all__ = ["ClientConfig", "ClientConfigError"]
