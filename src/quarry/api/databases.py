"""The databases contract: the single database the daemon is fixed to."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DatabaseInfo(BaseModel):
    """One named database's summary.

    The daemon is fixed to a single database, so the list returns exactly one
    entry.  ``extra="allow"`` keeps parity with the local
    ``discover_databases`` shape the CLI renders identically.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    document_count: int
    size_bytes: int
    size_description: str


class DatabaseList(BaseModel):
    """The database-list response envelope."""

    total_databases: int
    databases: list[DatabaseInfo]
