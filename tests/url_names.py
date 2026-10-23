"""The stored name of a URL that has a query, written out from the spec.

A second implementation of ``HttpUrl``'s query digest, so a test can state the
expected name without calling the code under test: the parameters sorted, joined
with ``&``, hashed with SHA-256, and the first 16 hex digits kept.
"""

from __future__ import annotations

import hashlib


def with_query(base: str, query: str) -> str:
    """Return the stored name of *base* (a name without a query) plus *query*."""
    normalized = "&".join(sorted(part for part in query.split("&") if part))
    return f"{base}?q={hashlib.sha256(normalized.encode()).hexdigest()[:16]}"
