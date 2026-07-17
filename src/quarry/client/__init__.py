"""Client-tier package: resolve a daemon target and drive its REST API.

Layer 2 of DES-031 v2.2 — imports only ``quarry.api``/shared primitives, never
the engine and never presentation.  :class:`ClientConfig` turns a stored login
config into the URL + pinned CA + live bearer a client presents;
:class:`QuarryClient` marshals typed ``quarry.api`` models over that target and
raises typed :class:`QuarryError` leaves the command layer maps to exit codes.
"""

from __future__ import annotations

from quarry.client.client import QuarryClient
from quarry.client.config import ClientConfig, ClientConfigError
from quarry.client.errors import (
    HttpError,
    QuarryConnectionError,
    QuarryError,
)
from quarry.client.resolver import TargetResolver
from quarry.client.task import TaskOutcome
from quarry.client.transport import HttpxTransport, Response, Transport

__all__ = [
    "ClientConfig",
    "ClientConfigError",
    "HttpError",
    "HttpxTransport",
    "QuarryClient",
    "QuarryConnectionError",
    "QuarryError",
    "Response",
    "TargetResolver",
    "TaskOutcome",
    "Transport",
]
