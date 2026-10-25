"""Find the documents an older build stored under a URL's former spelling."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Self, final
from urllib.parse import urlsplit

from quarry.display_name import DisplayName
from quarry.http_url import HttpUrl

if TYPE_CHECKING:
    from quarry.db.facade import Database

logger = logging.getLogger(__name__)

# What a LanceDB call raises on a fault: the codebase's convention.
_STORE_ERRORS = (OSError, RuntimeError, ValueError)


@final
class LegacyUrlNames:
    """Treat a document stored under an old URL spelling as the same document.

    Earlier builds stored a URL under names this one no longer produces: with its
    raw query or fragment, its default port, or its path unencoded.  Re-ingesting the
    URL must replace that document, not leave it orphaned beside the new one, and
    a dedup or lookup must see it as existing.  A stored name is *legacy* when it
    differs from the canonical name but reads as it, either
    as a stored name (:meth:`HttpUrl.stored`, which keeps a digest marker) or as a
    real URL (:attr:`HttpUrl.document_name`, where a query that spells the marker
    is just a query).  The search is bounded by the name's
    ``scheme://host`` prefix, so it reads only that host's documents.

    Only a document fetched from a URL has a legacy spelling: a stored candidate
    must itself be URL-sourced, and the page being stored must be too.  A document
    that someone named with a URL (``remember``, ``learn``) is a label and is never
    matched.  The bounds of the match: a host that differs only in case matches;
    a name with userinfo, or with the ideographic full stops IDNA reads as ``.``
    in its host, does not: any build that kept URL names refused the first and
    never stored the second.

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
            return HttpUrl.stored(name)
        except ValueError:
            return name

    @staticmethod
    def _recanonical(name: str) -> str:
        """Return *name* read as a real URL (a query that spells the marker is one)."""
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
            other for other in stored if other != name and self._spells(other, name)
        ]

    @classmethod
    def _spells(cls, other: str, name: str) -> bool:
        """Return whether the stored *other* is a former spelling of *name*.

        A name this build writes is already canonical and spells only itself: its
        marker is a digest, not a query, so reading it as a URL would make a
        copied digest alias a different page.
        """
        canonical = cls.canonical(other)
        return name == canonical or (
            canonical != other and name == cls._recanonical(other)
        )

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
                    self._database.url_names.names(prefix, collection)
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

    def stored_as(self, name: str, collection: str) -> str | None:
        """Return the name *name* is stored under in *collection*, or ``None``.

        That is *name* itself when it is stored, else the first legacy spelling of
        it.  A caller that hands the name on (for ``show``, say) must hand on this
        one: the legacy spelling is the only name the store knows.
        """
        if self._database.catalog.document_exists(name, collection):
            return name
        legacy = self.aliases(name, collection)
        return legacy[0] if legacy else None

    def exists(self, name: str, collection: str) -> bool:
        """Return whether *name*, or a legacy spelling of it, is stored."""
        return self.stored_as(name, collection) is not None

    def replace_prior(self, name: str, collection: str, *, overwrite: bool) -> None:
        """Delete *name* from *collection* when *overwrite*, before the insert."""
        if overwrite:
            self._database.store.delete_document(
                name, collection=collection, count=False
            )

    def drop_twins(self, name: str, collection: str) -> None:
        """Delete the legacy spellings of *name* once *name* is stored.

        Called only for a page fetched from a URL, overwriting or not: a legacy
        spelling is the same page under a name this build no longer writes, and
        the new document must not sit beside it.  It runs after the insert, so a
        failed embed or write leaves the old copy in place.  A document someone
        named with a URL is a label and is never touched.
        """
        try:
            twins = self.aliases(name, collection)
        except _STORE_ERRORS:
            logger.exception(
                "stored %s but could not look up its legacy-named twins; "
                "re-ingest with overwrite to replace them",
                DisplayName(name).redacted(),
            )
            return
        for legacy in twins:
            logger.warning(
                "replacing legacy-named document %s with %s",
                DisplayName(legacy).redacted(),
                DisplayName(name).redacted(),
            )
            try:
                self._database.store.delete_document(
                    legacy, collection=collection, count=False
                )
            except _STORE_ERRORS:
                # The page is stored; a twin that stays is a duplicate, not a loss.
                logger.exception(
                    "stored %s but could not delete its legacy-named twin %s; "
                    "re-ingest with overwrite to replace it",
                    DisplayName(name).redacted(),
                    DisplayName(legacy).redacted(),
                )
                continue
            self.dropped([legacy], collection)
