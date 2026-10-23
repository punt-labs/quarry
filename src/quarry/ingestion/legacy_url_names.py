"""Find the documents an older build stored under a URL's former spelling."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final
from urllib.parse import urlsplit

from quarry.db.name_prefix import NamePrefix
from quarry.display_name import DisplayName
from quarry.http_url import HttpUrl

if TYPE_CHECKING:
    from quarry.db.facade import Database

logger = logging.getLogger(__name__)


@final
class LegacyUrlNames:
    """Treat a document stored under an old URL spelling as the same document.

    Earlier builds stored a URL under names this one no longer produces: with its
    raw query or fragment, its default port, or its path unencoded.  Re-ingesting the
    URL must replace that document, not leave it orphaned beside the new one, and
    a dedup or lookup must see it as existing.  A stored name is *legacy* when it
    differs from the canonical name but canonicalizes to it through
    :attr:`HttpUrl.document_name`.  The search is bounded by the name's
    ``scheme://host`` prefix, so it reads only that host's documents.
    """

    __slots__ = ("_database",)

    _database: Database

    def __new__(cls, database: Database) -> Self:
        self = super().__new__(cls)
        self._database = database
        return self

    @staticmethod
    def canonical(name: str) -> str:
        """Return *name* as this build would store it; a non-URL name is as it is."""
        try:
            return HttpUrl(name).document_name
        except ValueError:
            return name

    def aliases(self, name: str, collection: str) -> list[str]:
        """Return the stored names in *collection* that spell *name* the old way."""
        try:
            parts = urlsplit(name)
        except ValueError as exc:
            # A scrubbed host (``[REDACTED:hostname]``) reads as a broken IPv6
            # bracket: no build stored such a name under another spelling.
            logger.debug(
                "no legacy spelling for an unparseable name: %s", type(exc).__name__
            )
            return []
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return []
        prefix = f"{parts.scheme}://{parts.netloc}"
        stored = NamePrefix(self._database.db).names(prefix, collection)
        return [
            other for other in stored if other != name and self.canonical(other) == name
        ]

    def exists(self, name: str, collection: str) -> bool:
        """Return whether *name*, or a legacy spelling of it, is stored."""
        return self._database.catalog.document_exists(name, collection) or bool(
            self.aliases(name, collection)
        )

    def replaced(self, name: str, collection: str) -> list[str]:
        """Return the legacy spellings of *name* an overwrite replaces, logging each."""
        found = self.aliases(name, collection)
        for legacy in found:
            logger.info(
                "replacing legacy-named document %s with %s",
                DisplayName(legacy).redacted(),
                DisplayName(name).redacted(),
            )
        return found
