"""Query and QuerySet: the committed evaluation queries and their qrels.

A query file is JSONL, one object per line:
``{"id", "text", "bucket", "split", "answer": {"document_name", "page_number"}}``
where ``bucket`` is natural/known-item/regression, ``split`` is dev/test, and
``answer`` (optional) is the known-item JudgedUnit, ``page_number`` null for
document granularity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from tools.eval.judged_unit import JudgedUnit
from tools.eval.trec import Qrels

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_BUCKETS = frozenset({"natural", "known-item", "regression"})
_SPLITS = frozenset({"dev", "test"})


@dataclass(frozen=True, slots=True)
class Query:
    """One evaluation query, its bucket/split, and optional known-item answer."""

    query_id: str
    text: str
    bucket: str
    split: str
    answer: JudgedUnit | None = None

    @classmethod
    def from_json(cls, obj: dict[str, object]) -> Self:
        """Build a query from one parsed JSONL object, validating at the boundary."""
        query_id = _require_str(obj, "id")
        bucket = _require_str(obj, "bucket")
        if bucket not in _BUCKETS:
            msg = f"query {query_id!r}: bucket {bucket!r} not in {sorted(_BUCKETS)}"
            raise ValueError(msg)
        split = _require_str(obj, "split")
        if split not in _SPLITS:
            msg = f"query {query_id!r}: split {split!r} not in {sorted(_SPLITS)}"
            raise ValueError(msg)
        return cls(
            query_id=query_id,
            text=_require_str(obj, "text"),
            bucket=bucket,
            split=split,
            answer=_parse_answer(obj.get("answer"), query_id),
        )


def _require_str(obj: dict[str, object], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        msg = f"query field {key!r} must be a non-empty string, got {value!r}"
        raise ValueError(msg)
    return value


def _parse_answer(raw: object, query_id: str) -> JudgedUnit | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        msg = f"query {query_id!r}: answer must be an object, got {raw!r}"
        raise ValueError(msg)
    document_name = raw.get("document_name")
    if not isinstance(document_name, str) or not document_name:
        msg = f"query {query_id!r}: answer.document_name must be a non-empty string"
        raise ValueError(msg)
    page = raw.get("page_number")
    if page is not None and not isinstance(page, int):
        msg = f"query {query_id!r}: answer.page_number must be an int or null"
        raise ValueError(msg)
    return JudgedUnit(document_name=document_name, page_number=page)


class QuerySet:
    """The loaded query file: iterable, bucket-groupable, and qrels-producing."""

    __slots__ = ("_queries",)

    _queries: tuple[Query, ...]

    def __new__(cls, queries: tuple[Query, ...]) -> Self:
        self = super().__new__(cls)
        self._queries = queries
        return self

    def __iter__(self) -> Iterator[Query]:
        return iter(self._queries)

    def __len__(self) -> int:
        return len(self._queries)

    @classmethod
    def from_path(cls, path: Path) -> Self:
        """Load and validate every JSONL line into a Query.

        Query ids must be unique: they key the qrels dict and per-query scores,
        so a duplicate would silently overwrite an earlier query. A repeated id
        is rejected at load time, naming the offender.
        """
        queries: list[Query] = []
        seen: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                msg = f"query line is not a JSON object: {line!r}"
                raise ValueError(msg)
            query = Query.from_json(obj)
            if query.query_id in seen:
                msg = (
                    f"duplicate query id {query.query_id!r} in {path}: ids must "
                    "be unique because they key qrels and per-query scores"
                )
                raise ValueError(msg)
            seen.add(query.query_id)
            queries.append(query)
        return cls(tuple(queries))

    def buckets(self) -> list[str]:
        """Return the buckets present, in the canonical report order."""
        present = {q.bucket for q in self._queries}
        return [b for b in ("natural", "known-item", "regression") if b in present]

    def in_bucket(self, bucket: str) -> list[Query]:
        """Return the queries in one bucket, preserving file order."""
        return [q for q in self._queries if q.bucket == bucket]

    def scorable(self) -> list[Query]:
        """Return queries carrying a known-item answer (the Phase-1 qrels set)."""
        return [q for q in self._queries if q.answer is not None]

    def to_qrels(self) -> Qrels:
        """Build binary qrels from every query that carries an answer."""
        judgments = {
            q.query_id: {q.answer.docid: 1}
            for q in self._queries
            if q.answer is not None
        }
        return Qrels(judgments)
