"""The labels a daemon ingest carries: where it writes and how it is tagged."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from quarry.ingestion import sitemap_ingest, web_ingest
from quarry.ingestion.bulk_ingest import BulkOptions
from quarry.ingestion.existing_page import ExistingPage
from quarry.ingestion.ingest_context import IngestContext, MemoryType, Progress

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.context import DaemonContext
    from quarry.http_url import HttpUrl


@final
@dataclass(frozen=True, slots=True)
class IngestLabels:
    """Where an ingest writes and how it is tagged, validated once at the route.

    Routes build this from the request and the jobs carry it unchanged, so the
    narrowing of ``memory_type`` to its storage vocabulary happens exactly once
    (:class:`MemoryTypeGuard`) and every job turns it into the pipeline's
    :class:`IngestContext` the same way.
    """

    overwrite: bool
    collection: str
    agent_handle: str
    memory_type: MemoryType
    summary: str

    def context(self, ctx: DaemonContext) -> IngestContext:
        """Return the pipeline context writing to *ctx*'s database with these labels."""
        return IngestContext(
            ctx.database,
            ctx.settings,
            overwrite=self.overwrite,
            collection=self.collection,
            agent_handle=self.agent_handle,
            memory_type=self.memory_type,
            summary=self.summary,
        )

    def plain_ingest(self, ctx: DaemonContext, source: HttpUrl) -> dict[str, object]:
        """Ingest *source* as ``quarry ingest`` does: sitemap-aware, unscrubbed."""
        context = self.context(ctx)
        return dict(
            sitemap_ingest.ingest_auto(source, Progress(None), context, BulkOptions())
        )

    def existing(
        self, ctx: DaemonContext, name: str, *, url_sourced: bool
    ) -> ExistingPage | None:
        """Return the held document that makes ingesting *name* a skip, if any."""
        find = ExistingPage.page if url_sourced else ExistingPage.label
        return find(name, self.context(ctx))

    @staticmethod
    def stored_name(
        name: str, scrub: Callable[[str], str], *, url_sourced: bool
    ) -> str:
        """Return the name an inline ingest stores *name* under; safe to log."""
        return web_ingest.InlineIngest(
            "", content_scrubber=scrub, url_sourced=url_sourced
        ).stored_name(name)
