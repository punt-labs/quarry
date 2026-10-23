"""Find the documents an older build stored under a URL's former spelling."""

from __future__ import annotations

import logging
import threading
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

    An instance remembers each host's listing, so one made for a bulk run lists a
    host once however many of its pages the run stores; one made for a single
    call lists it once too.  Pages the run replaces are dropped from the listing
    and pages it stores are added, so a later page never sees a stale name.  It is
    safe to share between the workers of a run.
    """

    __slots__ = ("_database", "_listed", "_lock")

    _database: Database
    _listed: dict[tuple[str, str], set[str]]
    _lock: threading.Lock

    def __new__(cls, database: Database) -> Self:
        self = super().__new__(cls)
        self._database = database
        self._listed = {}
        self._lock = threading.Lock()
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
        stored = self._under(f"{parts.scheme}://{parts.netloc}", collection)
        return [
            other for other in stored if other != name and self.canonical(other) == name
        ]

    def stored(self, name: str, collection: str) -> None:
        """Note that *name* is now stored in *collection*, if its host is listed."""
        key = self._key(name, collection)
        with self._lock:
            if key in self._listed:
                self._listed[key].add(name)

    def dropped(self, names: list[str], collection: str) -> None:
        """Note that *names* are no longer stored in *collection*."""
        with self._lock:
            for name in names:
                self._listed.get(self._key(name, collection), set()).discard(name)

    def _under(self, prefix: str, collection: str) -> list[str]:
        """Return the stored names under *prefix*, listing the host the first time."""
        key = (collection, prefix.lower())
        with self._lock:
            if key not in self._listed:
                self._listed[key] = set(
                    NamePrefix(self._database.db).names(prefix, collection)
                )
            return sorted(self._listed[key])

    @staticmethod
    def _key(name: str, collection: str) -> tuple[str, str]:
        """Return the listing *name* belongs to: its collection and ``scheme://host``."""
        try:
            parts = urlsplit(name)
        except ValueError:
            return collection, ""
        return collection, f"{parts.scheme}://{parts.netloc}".lower()

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
