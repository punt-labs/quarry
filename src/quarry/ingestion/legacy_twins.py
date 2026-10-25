"""Delete the legacy-named twins of a page once the page is stored."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.display_name import DisplayName
from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from quarry.db.facade import Database
    from quarry.ingestion.legacy_url_names import LegacyUrlNames

logger = logging.getLogger(__name__)


@final
class LegacyTwins:
    """Drop the legacy spellings of a stored page, so a re-ingest leaves one.

    A legacy spelling is the same page under a name this build no longer writes,
    and the new document must not sit beside it.  The page is already stored when
    this runs, so no fault in the lookup or the delete, whatever its class, may
    fail the ingest: a twin that stays is a duplicate, not a loss.  Each fault is
    logged as its class and frames, never its message, which can quote a name.
    """

    __slots__ = ("_database", "_names")

    _names: LegacyUrlNames
    _database: Database

    def __new__(cls, names: LegacyUrlNames, database: Database) -> Self:
        self = super().__new__(cls)
        self._names = names
        self._database = database
        return self

    def drop(self, name: str, collection: str) -> None:
        """Delete the legacy spellings of *name* from *collection*."""
        try:
            twins = self._names.aliases(name, collection)
        except Exception as exc:  # a cleanup boundary: see the class docstring
            logger.error(
                "stored %s but could not look up its legacy-named twins; "
                "re-ingest with overwrite to replace them",
                DisplayName(name).redacted(),
                exc_info=RedactedFailure.of(exc),
            )
            return
        for legacy in twins:
            logger.warning(
                "replacing legacy-named document %s with %s",
                DisplayName(legacy).redacted(),
                DisplayName(name).redacted(),
            )
            self._delete(name, legacy, collection)

    def _delete(self, name: str, legacy: str, collection: str) -> None:
        """Delete one twin; on a fault leave it, and the listing, as they were."""
        try:
            self._database.store.delete_document(
                legacy, collection=collection, count=False
            )
        except Exception as exc:  # a cleanup boundary: see the class docstring
            logger.error(
                "stored %s but could not delete its legacy-named twin %s; "
                "re-ingest with overwrite to replace it",
                DisplayName(name).redacted(),
                DisplayName(legacy).redacted(),
                exc_info=RedactedFailure.of(exc),
            )
            return
        self._names.dropped([legacy], collection)
