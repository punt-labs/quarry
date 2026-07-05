"""Indexing and page-collapse: build the ephemeral index, collapse chunks to units.

Both concerns sit *between* the fixture and the run: ``EphemeralIndex`` ingests
the corpus once per embedding signature, and ``Collapser`` folds the retriever's
per-chunk results into the page/document-keyed, deterministic ranking the run
records. Neither touches the frozen retriever; ``EvalRunner`` (runner.py) drives
both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from quarry.config import ONNX_MODEL_REVISION
from quarry.db.facade import Database
from quarry.db.schema import TABLE_NAME
from tools.eval.judged_unit import JudgedUnit

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from quarry.config import Settings
    from quarry.results import SearchResult
    from tools.eval.corpus import Corpus


class Collapser:
    """Collapse per-chunk results to a page/document-keyed deterministic ranking."""

    __slots__ = ()

    @staticmethod
    def collapse(
        results: Sequence[SearchResult], *, page_level: bool
    ) -> list[tuple[str, float]]:
        """Return ``(docid, score)`` rows: best chunk per unit, deterministic order.

        The first time a unit's docid appears it is at its best (highest) rank,
        because the retriever returns results in fused-rank order. We keep that
        position and re-sort by ``(position, docid)`` so equal positions (which
        cannot occur here, but the contract demands an explicit tie-break) break
        deterministically by JudgedUnit docid. Score encodes rank as
        ``1/(position+1)`` so it is strictly decreasing.
        """
        best_position: dict[str, int] = {}
        for position, result in enumerate(results):
            docid = JudgedUnit.from_result(result, page_level=page_level).docid
            if docid not in best_position:
                best_position[docid] = position
        ordered = sorted(best_position.items(), key=lambda kv: (kv[1], kv[0]))
        return [(docid, 1.0 / (pos + 1)) for docid, pos in ordered]


class EphemeralIndex:
    """A per-embedding-signature LanceDB index under the ephemeral work dir.

    The index is keyed on the embedding strategy, the pinned model revision, and
    the corpus content hash, so configs that do not change embeddings
    (metadata/fusion/reranker knobs) reuse one index — the dominant cost saving.
    A model-distinct config (Phase 2+) gets a different key and re-indexes.
    """

    __slots__ = ("_corpus", "_key", "_root", "_settings")

    _root: Path
    _corpus: Corpus
    _settings: Settings
    _key: str

    def __new__(
        cls, root: Path, corpus: Corpus, settings: Settings, embedding_strategy: str
    ) -> Self:
        self = super().__new__(cls)
        self._root = root
        self._corpus = corpus
        self._settings = settings
        revision = ONNX_MODEL_REVISION[:8]
        self._key = f"{embedding_strategy}-{revision}-{corpus.content_signature()}"
        return self

    @property
    def key(self) -> str:
        """The cache key identifying this index (embedding + model + corpus)."""
        return self._key

    def database(self) -> Database:
        """Return a populated Database, ingesting the corpus once per key."""
        path = self._root / self._key / "lancedb"
        database = Database.connect(path)
        if self._populated(database):
            return database
        database.ensure_schema()
        self._corpus.ingest_into(database, self._settings)
        return database

    @staticmethod
    def _populated(database: Database) -> bool:
        raw = database.db
        if TABLE_NAME not in raw.list_tables().tables:
            return False
        return bool(raw.open_table(TABLE_NAME).count_rows() > 0)
