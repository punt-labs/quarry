"""Shared SQL helpers for LanceDB predicate construction."""

from __future__ import annotations


def escape_sql(value: str) -> str:
    """Escape single quotes for LanceDB SQL predicates."""
    return value.replace("'", "''")
