"""The ``/v1/registrations`` contract: register a directory and list registrations."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RegisterRequest(BaseModel):
    """Body for ``POST /v1/registrations`` — track a directory for sync.

    ``directory`` must resolve inside the daemon's home directory; the daemon
    rejects traversal and out-of-tree paths before registering.
    """

    directory: str
    collection: str


class RegistrationInfo(BaseModel):
    """One directory registration.

    ``extra="allow"`` keeps the model a superset of the registry row shape.
    """

    model_config = ConfigDict(extra="allow")

    collection: str
    directory: str
    registered_at: str


class RegistrationList(BaseModel):
    """The ``GET /v1/registrations`` response envelope."""

    total_registrations: int
    registrations: list[RegistrationInfo]
