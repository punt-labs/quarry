"""One document's shared version and whether it was stored completely."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@final
@dataclass(frozen=True, slots=True)
class DocumentVersion:
    """A document's shared ``source_version`` and its page completeness.

    ``version`` is ``""`` when the document has no rows, or its rows disagree
    on version — two overwrites of one document that interleave can leave
    rows from both, and such a document vouches for no single version. A
    version match alone does not prove the document is whole: a windowed
    insert that failed partway through leaves surviving rows correctly
    tagged with the version being written, but short of ``total_pages``
    worth of indexed pages — :attr:`complete` is what a caller must also
    check before treating a version match as "nothing to do".
    """

    version: str
    total_pages: int
    indexed_pages: int

    @classmethod
    def absent(cls) -> Self:
        """Return the version of a document with no stored rows at all."""
        return cls("", 0, 0)

    @classmethod
    def of_rows(cls, rows: Sequence[Mapping[str, object]]) -> Self:
        """Summarise one document's version and completeness from its *rows*.

        *rows* must already be filtered to a single ``document_name`` and
        ``collection`` — this does no grouping of its own.
        """
        if not rows:
            return cls.absent()
        versions = {str(r.get("source_version", "")) for r in rows}
        version = versions.pop() if len(versions) == 1 else ""
        return cls(
            version=version,
            total_pages=int(str(rows[0]["total_pages"])),
            indexed_pages=len({int(str(r["page_number"])) for r in rows}),
        )

    @property
    def complete(self) -> bool:
        """Return whether every page the document claims was indexed.

        ``total_pages == 0`` (no rows) is never complete, even though
        ``0 == 0`` would otherwise say so.
        """
        return self.total_pages > 0 and self.indexed_pages == self.total_pages

    def matches_complete(self, version: str) -> bool:
        """Return whether *version* names this document AND it is whole.

        The compound check an unchanged-capture gate needs: a version match
        on a partial document (a windowed insert that failed partway
        through) must not count as "nothing to do," or a retry of the same
        unchanged file would match the same version forever and the missing
        chunks would never be repaired.
        """
        return self.version == version and self.complete
