"""TREC-format run and qrels value objects, joined on JudgedUnit.docid.

Both keep their nested-mapping representation private and expose typed
behavior. ``ranx`` is imported lazily inside ``to_ranx`` so importing this
module never requires the eval-only dependency (CI installs no ``eval`` extra).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from ranx import Qrels as RanxQrels, Run as RanxRun

# TREC run column 2 is a historical, ignored constant; qrels column 2 is 0.
_RUN_ITER = "Q0"
_QREL_ITER = "0"


class TrecRun:
    """A ranked run: for each query, an ordered list of ``(docid, score)``.

    The stored order *is* the ranking — position 0 is rank 1. Determinism is
    the caller's contract (the runner sorts by score then docid before
    building), so this object never re-sorts by score and cannot flip a tie.
    ``to_ranx`` re-encodes rank as a strictly-decreasing synthetic score so
    ranx measures exactly this order, whatever the raw fused scores were.
    """

    __slots__ = ("_rankings", "_tag")

    _rankings: dict[str, list[tuple[str, float]]]
    _tag: str

    def __new__(
        cls, rankings: Mapping[str, Sequence[tuple[str, float]]], tag: str
    ) -> Self:
        self = super().__new__(cls)
        self._rankings = {q: list(rows) for q, rows in rankings.items()}
        self._tag = tag
        return self

    @property
    def tag(self) -> str:
        """The run tag written to TREC column 6."""
        return self._tag

    @property
    def query_ids(self) -> list[str]:
        """Query ids present in the run, in insertion order."""
        return list(self._rankings)

    def ranking(self, query_id: str) -> list[tuple[str, float]]:
        """Return the ranked ``(docid, score)`` rows for one query."""
        return list(self._rankings[query_id])

    def subset(self, query_ids: Iterable[str]) -> Self:
        """Return a run restricted to *query_ids* that are present."""
        wanted = set(query_ids)
        rankings = {q: rows for q, rows in self._rankings.items() if q in wanted}
        return type(self)(rankings, self._tag)

    def write(self, path: Path) -> None:
        """Write six-column TREC run lines: ``qid Q0 docid rank score tag``."""
        lines: list[str] = []
        for query_id, rows in self._rankings.items():
            for position, (docid, score) in enumerate(rows):
                rank = position + 1
                lines.append(
                    f"{query_id} {_RUN_ITER} {docid} {rank} {score:.6f} {self._tag}"
                )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @classmethod
    def from_path(cls, path: Path) -> Self:
        """Parse a six-column TREC run file, ordering each query by rank column.

        Every line must carry the same run tag (column 6). A file mixing tags
        has no single well-defined ``TrecRun.tag``, so it is rejected at load
        time rather than silently adopting whichever tag came last.
        """
        by_query: dict[str, list[tuple[int, str, float]]] = {}
        tag: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            query_id, _iter, docid, rank, score, line_tag = line.split()
            if tag is None:
                tag = line_tag
            elif line_tag != tag:
                msg = (
                    f"TREC run file {path} mixes run tags: found {line_tag!r} "
                    f"after {tag!r}. Every line must share one tag."
                )
                raise ValueError(msg)
            by_query.setdefault(query_id, []).append((int(rank), docid, float(score)))
        rankings = {
            query_id: [(docid, score) for _rank, docid, score in sorted(rows)]
            for query_id, rows in by_query.items()
        }
        return cls(rankings, tag or "")

    def to_ranx(self) -> RanxRun:
        """Build a ranx Run whose scores encode this object's exact order.

        Queries with an empty ranking are omitted: ranx cannot represent a
        zero-document query. A query that retrieved nothing is therefore absent
        from the run — it is scored as a miss against its qrels ONLY when the
        caller passes ``make_comparable=True`` to ``ranx.evaluate`` (Scorer does).
        Without that flag ranx's ``check_keys`` raises on the qrel/run mismatch
        instead of scoring the miss.
        """
        from ranx import Run  # noqa: PLC0415

        encoded: dict[str, dict[str, float]] = {
            query_id: {
                docid: float(len(rows) - position)
                for position, (docid, _score) in enumerate(rows)
            }
            for query_id, rows in self._rankings.items()
            if rows
        }
        return Run(encoded, name=self._tag)


class Qrels:
    """Binary or graded relevance judgments keyed on ``JudgedUnit.docid``."""

    __slots__ = ("_judgments",)

    _judgments: dict[str, dict[str, int]]

    def __new__(cls, judgments: Mapping[str, Mapping[str, int]]) -> Self:
        self = super().__new__(cls)
        self._judgments = {q: dict(rels) for q, rels in judgments.items()}
        return self

    @property
    def query_ids(self) -> list[str]:
        """Query ids that carry at least one judgment."""
        return list(self._judgments)

    def relevant_docids(self, query_id: str) -> set[str]:
        """Return the docids judged relevant (grade >= 1) for one query."""
        return {d for d, rel in self._judgments.get(query_id, {}).items() if rel >= 1}

    def judged_docids(self, query_id: str) -> set[str]:
        """Return every docid carrying a judgment (any grade) for one query."""
        return set(self._judgments.get(query_id, {}))

    def subset(self, query_ids: Iterable[str]) -> Self:
        """Return qrels restricted to *query_ids* that are present."""
        wanted = set(query_ids)
        judgments = {q: rels for q, rels in self._judgments.items() if q in wanted}
        return type(self)(judgments)

    def write(self, path: Path) -> None:
        """Write four-column TREC qrels lines: ``qid 0 docid relevance``."""
        lines = [
            f"{query_id} {_QREL_ITER} {docid} {rel}"
            for query_id, rels in self._judgments.items()
            for docid, rel in rels.items()
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @classmethod
    def from_path(cls, path: Path) -> Self:
        """Parse a four-column TREC qrels file."""
        judgments: dict[str, dict[str, int]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            query_id, _iter, docid, rel = line.split()
            judgments.setdefault(query_id, {})[docid] = int(rel)
        return cls(judgments)

    def to_ranx(self) -> RanxQrels:
        """Build a ranx Qrels from the judgments."""
        from ranx import Qrels as RanxQrelsClass  # noqa: PLC0415

        return RanxQrelsClass(self._judgments)
