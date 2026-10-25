"""The ingest context shared by every page of one bulk run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from quarry.ingestion.ingest_context import IngestContext
from quarry.ingestion.legacy_url_names import LegacyUrlNames


@final
@dataclass(frozen=True, slots=True)
class RunContext(IngestContext):
    """An :class:`IngestContext` that also carries the run's listing of stored names.

    The pages of a crawl share it, so a host's stored names are listed once for the
    run, not once per page, and a name one page replaces is gone for the next.
    """

    names: LegacyUrlNames | None = None

    @classmethod
    def for_run(cls, context: IngestContext, *, overwrite: bool) -> RunContext:
        """Return *context*'s knobs, with *overwrite*, and a fresh listing."""
        return cls(
            context.database,
            context.settings,
            overwrite=overwrite,
            collection=context.collection,
            agent_handle=context.agent_handle,
            memory_type=context.memory_type,
            summary=context.summary,
            source_version=context.source_version,
            names=LegacyUrlNames(context.database),
        )

    @staticmethod
    def legacy_names_of(context: IngestContext) -> LegacyUrlNames:
        """Return the listing of the run *context* belongs to, or a fresh one."""
        if isinstance(context, RunContext) and context.names is not None:
            return context.names
        return LegacyUrlNames(context.database)

    @staticmethod
    def note_stored(context: IngestContext, name: str) -> None:
        """Tell the run's listing, if *context* has one, that *name* is stored."""
        if isinstance(context, RunContext) and context.names is not None:
            context.names.stored(name, context.collection)
