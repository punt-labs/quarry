"""Choose the queue a database's maintenance work rides, with or without a directory."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.route_key import RouteKey

if TYPE_CHECKING:
    from quarry.daemon.watch_roster import WatchRoster

logger = logging.getLogger(__name__)

# The routing key for finalize work on a database with no registered directory.
_MAINTENANCE_COLLECTION = "__maintenance__"


@final
class MaintenanceKey:
    """Pick the route key for a database's maintenance finalize.

    A registered collection's FIFO is used when the database has one, so the
    finalize queues behind that collection's jobs.  A database with no directory,
    or one whose registry cannot be read, uses the reserved maintenance
    collection.  An unreadable registry is logged, never silent, and never stops
    the work: the finalize matters more than which queue carries it.
    """

    __slots__ = ("_roster",)

    _roster: WatchRoster

    def __new__(cls, roster: WatchRoster) -> Self:
        """Bind the choice to the roster that knows the registrations."""
        self = super().__new__(cls)
        self._roster = roster
        return self

    def of(self, database: str) -> RouteKey:
        """Return the route key for *database*'s maintenance work."""
        try:
            self._roster.ensure_database(database)
            registered = self._roster.registrations(database)
        except (OSError, ValueError, sqlite3.Error) as exc:
            logger.warning(
                "maintenance for %s: directory registry unreadable (%s); "
                "falling back to the %s queue",
                database,
                exc,
                _MAINTENANCE_COLLECTION,
            )
            registered = []
        collection = registered[0][0] if registered else _MAINTENANCE_COLLECTION
        return RouteKey(database, collection)
