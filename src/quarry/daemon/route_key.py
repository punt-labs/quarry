"""The ingest queue's composite routing key: a collection within a database."""

from __future__ import annotations

from dataclasses import dataclass
from typing import final


@final
@dataclass(frozen=True, slots=True)
class RouteKey:
    """The queue's routing key: a collection scoped to the database that owns it.

    A LanceDB table lives in exactly one database, so ``(database, collection)``
    names one table uniquely across the whole roster.  Keying the per-collection
    FIFO worker on this composite key extends the single-writer-per-table
    invariant across every watched database (DES-045 §14.1): two units on the
    same ``(database, collection)`` run in submission order, while the *same*
    collection name in two different databases routes to two independent workers
    (they are two tables — no false serialization, no cross-database race).
    """

    database: str
    collection: str
