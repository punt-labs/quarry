"""A page the store already holds, and the skip a create-if-absent ingest reports."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from quarry.display_name import DisplayName
from quarry.ingestion.run_context import RunContext
from quarry.web_fetch_name import WebFetchName

if TYPE_CHECKING:
    from quarry.http_url import HttpUrl
    from quarry.ingestion.ingest_context import IngestContext
    from quarry.ingestion.legacy_url_names import LegacyUrlNames
    from quarry.results import IngestResult

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class ExistingPage:
    """A document a create-if-absent ingest found stored, under the name it holds.

    The name the store holds is not always the name asked for: a build that spelled
    the URL differently stored the page under its own spelling, and a caller that
    hands the name on (a skip's ``document_name``, for ``show``) must hand on that
    one.  Every ingest that skips a held page reports it through :meth:`skip`, so
    the result and the log line agree whichever door the ingest came through.
    """

    name: str
    collection: str

    @classmethod
    def look_for(
        cls, names: LegacyUrlNames, name: str, collection: str
    ) -> ExistingPage | None:
        """Return the page held as *name* or a legacy spelling of it, else ``None``.

        ``None`` is the lookup's contract for a free name (PY-EH-8): the caller
        writes.
        """
        held = names.stored_as(name, collection)
        return None if held is None else cls(held, collection)

    @classmethod
    def page(cls, name: str, context: IngestContext) -> ExistingPage | None:
        """Return the held page that makes a non-overwriting URL ingest skip.

        Only a page fetched from a URL has a legacy spelling to match, so a copy
        an older build stored under a former spelling counts.  An overwrite deletes
        the prior copy on purpose, so nothing blocks it.  ``None`` when none does.
        """
        if context.overwrite:
            return None
        names = RunContext.legacy_names_of(context)
        return cls.look_for(names, name, context.collection)

    @classmethod
    def capture(cls, source: HttpUrl, context: IngestContext) -> ExistingPage | None:
        """Return the stored capture of *source* that makes a re-ingest skip, if any.

        Raises:
            ValueError: *source* has no capture name; it is not read as free.
        """
        return cls.page(WebFetchName.of(source.fetch_url), context)

    @classmethod
    def label(cls, name: str, context: IngestContext) -> ExistingPage | None:
        """Return the held document that makes a non-overwriting label ingest skip.

        A name someone chose (``remember``, ``learn``) is a label, whatever its
        format hint: only that exact name counts.  ``None`` when none does.
        """
        if context.overwrite:
            return None
        if context.database.catalog.document_exists(name, context.collection):
            return cls(name, context.collection)
        return None

    def skip(self) -> IngestResult:
        """Log the skip and return the result a create-if-absent ingest reports."""
        logger.info(
            "%s already stored; pass --overwrite to refresh",
            DisplayName(self.name).redacted(),
        )
        return {
            "document_name": self.name,
            "collection": self.collection,
            "chunks": 0,
            "skipped": "exists",
        }
