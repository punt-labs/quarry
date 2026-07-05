"""Corpus: load the committed raw fixture docs and ingest them into a Database.

Follows the facade convention — it accepts an already-composed ``Database`` and
never re-wraps a raw LanceDB connection. The ingested index is an ephemeral,
per-config artifact the runner keys on ``content_signature``.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from quarry.config import Settings
    from quarry.db.facade import Database
    from quarry.results import IngestResult

# Fixture formats the corpus ingests. Kept explicit so an unrelated file dropped
# into the fixture directory is skipped, not fed to a mismatched handler.
_SUFFIXES = frozenset({".tex", ".md", ".txt", ".py", ".rst"})


class Corpus:
    """The set of raw fixture documents under one directory.

    Fixture basenames must be unique across every subdirectory: ``path.name`` is
    the ``document_name`` written to LanceDB and the join key for JudgedUnit and
    qrels. Two files sharing a basename would collide on ingest (last write wins,
    silently dropping a doc), so the constructor rejects them at load time.
    """

    __slots__ = ("_documents", "_root")

    _root: Path
    _documents: tuple[Path, ...]

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        self._documents = tuple(
            sorted(
                p
                for p in root.rglob("*")
                if p.is_file() and p.suffix.lower() in _SUFFIXES
            )
        )
        self._reject_duplicate_basenames()
        return self

    def _reject_duplicate_basenames(self) -> None:
        """Raise if any basename repeats — it is the document_name join key."""
        by_name: dict[str, list[Path]] = defaultdict(list)
        for path in self._documents:
            by_name[path.name].append(path)
        collisions = {name: paths for name, paths in by_name.items() if len(paths) > 1}
        if collisions:
            detail = "; ".join(
                f"{name}: {', '.join(str(p) for p in paths)}"
                for name, paths in sorted(collisions.items())
            )
            msg = (
                "Fixture basenames must be unique — they are the document_name "
                "join key for qrels/JudgedUnit. Colliding basenames under "
                f"{self._root}: {detail}"
            )
            raise ValueError(msg)

    @property
    def root(self) -> Path:
        """The fixture directory this corpus was loaded from."""
        return self._root

    @property
    def documents(self) -> tuple[Path, ...]:
        """The raw document paths, sorted for deterministic ingest order."""
        return self._documents

    def __len__(self) -> int:
        return len(self._documents)

    def content_signature(self) -> str:
        """Return a short hash of every document's name and bytes.

        Two corpora with identical content share an ephemeral index; editing a
        fixture changes the signature and forces a re-index.
        """
        digest = hashlib.sha256()
        for path in self._documents:
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()[:16]

    def ingest_into(
        self, database: Database, settings: Settings, *, collection: str = "eval"
    ) -> list[IngestResult]:
        """Ingest every fixture document into *database*, overwriting prior data."""
        from quarry.ingestion.pipeline import ingest_document  # noqa: PLC0415

        return [
            ingest_document(
                path,
                database,
                settings,
                overwrite=True,
                collection=collection,
                document_name=path.name,
            )
            for path in self._documents
        ]

    def document_names(self) -> Sequence[str]:
        """Return the stored document names (file names) in ingest order."""
        return [p.name for p in self._documents]
