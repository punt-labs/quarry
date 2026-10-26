"""Spec-derived URL names for tests, and a row stored the way an older build did.

A second implementation of ``HttpUrl``'s query digest, so a test can state the
expected name without calling the code under test: the parameters sorted, joined
with ``&``, hashed with SHA-256, and the first 16 hex digits kept.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from quarry.models import Chunk
from tests.fakes import FakeEmbeddingBackend

if TYPE_CHECKING:
    from quarry.db.facade import Database


def with_query(base: str, query: str) -> str:
    """Return the stored name of *base* (a name without a query) plus *query*."""
    normalized = "&".join(sorted(part for part in query.split("&") if part))
    return f"{base}?q[sha256]={hashlib.sha256(normalized.encode()).hexdigest()[:16]}"


def store_as_an_older_build_did(
    db: Database,
    name: str,
    *,
    collection: str = "c",
    url_sourced: bool = True,
) -> None:
    """Write a row under *name* exactly, as a build that spelled it differently did.

    A page fetched from a URL records that URL as its path; a document someone
    named with a URL (``remember``) records none.
    """
    chunk = Chunk(
        document_name=name,
        document_path=name if url_sourced else "",
        collection=collection,
        page_number=1,
        total_pages=1,
        chunk_index=0,
        text="old body",
        page_raw_text="old body",
        page_type="text",
        source_format="inline",
        ingestion_timestamp=datetime.now(tz=UTC),
    )
    vector = FakeEmbeddingBackend(768).embed_texts(["old body"])
    db.store.insert([chunk], vector)
