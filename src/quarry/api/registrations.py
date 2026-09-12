"""The registrations contract: register a directory and list registrations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RegisterRequest(BaseModel):
    """Body for tracking a directory for sync.

    ``directory`` must resolve inside the daemon's home directory; the daemon
    rejects traversal and out-of-tree paths before registering.
    """

    directory: str
    collection: str


class RegistrationInfo(BaseModel):
    """One directory registration.

    ``extra="allow"`` keeps the model a superset of the registry row shape.
    ``watch_state`` defaults to ``"scan-only"`` so a response from an older
    daemon that omits the field still parses as the conservative reading —
    "assume the safety scan is doing the work" — rather than as "watched"
    (DES-045e: :meth:`~quarry.daemon.watch_loop.WatchLoop.watch_state`).
    """

    model_config = ConfigDict(extra="allow")

    collection: str
    directory: str
    registered_at: str
    watch_state: Literal["watched", "degraded", "scan-only"] = "scan-only"


class RetainedCollection(BaseModel):
    """A collection whose chunks were kept on a keep-data disable (archived).

    ``original_directory`` is the directory it was registered under, so the
    client can tell whether THIS directory owns the archive: the same directory
    re-adopts it (reuses the name + kept chunks); a different directory must not
    (it would inherit another project's chunks).  Empty when a legacy marker
    recorded no origin — it then matches no directory and is never re-adopted.
    """

    model_config = ConfigDict(extra="allow")

    collection: str
    original_directory: str = ""


class RegistrationList(BaseModel):
    """The registration-list response envelope.

    ``retained`` carries the archived (keep-data) collections with the directory
    each was registered under.  The name-picker avoids their names so a new,
    unrelated directory never collides with an archive; the enable path re-adopts
    an archive whose ``original_directory`` matches the directory being enabled.

    ``chunk_collections`` carries every collection that currently holds chunks
    (the same catalog source the orphan sweep reads).  The name-picker avoids
    these too, so a DIFFERENT directory can never be auto-assigned a name that
    already holds another project's chunks — the auto path is structurally
    merge-proof.  Both fields default so a response from an older daemon that
    omits them still parses (wire-compat at the boundary).
    """

    total_registrations: int
    registrations: list[RegistrationInfo]
    retained: list[RetainedCollection] = Field(default_factory=list)
    chunk_collections: list[str] = Field(default_factory=list)
