"""Output formatting shared by quarry's CLI and MCP tool responses.

Adapts biff's constrained-width table formatter (DES-014) for quarry's
data types.  Data tools return pre-formatted plain text; action tools
return compact summary lines.  The MCP surface's PostToolUse hook routes
these to the UI panel and LLM context; the CLI prints them directly.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from quarry.table_layout import TABLE_WIDTH, ColumnSpec, TableLayout

# Data tool formatters ────────────────────────────────────────────────────────
# Mapping[str, Any]: data arrives from LanceDB row dicts, TypedDicts, and
# JSON-like structures with no single typed schema at this formatting boundary.


@dataclass(frozen=True, slots=True)
class _Listing:
    """A count-headed table listing (documents, collections, ...).

    The first line of the rendered output is an authoritative ``▶  N noun``
    summary; the plugin's PostToolUse hook shows that line in the panel. A
    count derived by counting rendered lines would over-count wrapped rows
    when a variable-width column overflows — the summary must come from the
    input length, not the output layout.
    """

    _noun: str
    _specs: list[ColumnSpec]

    def render(self, rows: list[list[str]]) -> str:
        r"""Return ``▶  N noun(s)\n<table>`` for *rows*."""
        n = len(rows)
        plural = self._noun if n == 1 else self._noun + "s"
        return f"▶  {n} {plural}\n{TableLayout(self._specs).render(rows)}"


def format_search_results(query: str, results: Sequence[Mapping[str, Any]]) -> str:
    """Format search results as a numbered list with text excerpts."""
    n = len(results)
    if n == 0:
        return f'No results for "{query}"'

    header = f'\u25b6  {n} result{"s" if n != 1 else ""} for "{query}"'
    lines = [header, ""]

    for i, r in enumerate(results, 1):
        doc = r.get("document_name", "?")
        page = r.get("page_number", "?")
        score = r.get("similarity", 0)
        text = r.get("text", "")

        lines.append(f"   {i}. {doc}  p{page}  [{score:.2f}]")
        if text:
            excerpt = TableLayout.truncate(text)
            # Indent excerpt lines under the result header
            wrapped = textwrap.wrap(excerpt, TABLE_WIDTH - 6)
            lines.extend(f"      {line}" for line in wrapped)
        lines.append("")

    return "\n".join(lines).rstrip()


def format_documents(docs: Sequence[Mapping[str, Any]]) -> str:
    """Format document listing as a count header followed by a table."""
    if not docs:
        return "No documents"
    listing = _Listing(
        "document",
        [
            ColumnSpec("DOCUMENT", 8, fixed=False),
            ColumnSpec("COLLECTION", 8),
            ColumnSpec("PAGES", 5, align="right"),
            ColumnSpec("CHUNKS", 6, align="right"),
        ],
    )
    return listing.render(
        [
            [
                d.get("document_name", "?"),
                d.get("collection", "?"),
                str(d.get("total_pages", 0)),
                str(d.get("chunk_count", 0)),
            ]
            for d in docs
        ]
    )


def format_document_detail(doc: Mapping[str, Any]) -> str:
    """Format single document metadata for show command."""
    lines = [
        f"Document: {doc.get('document_name', '?')}",
        f"Collection: {doc.get('collection', '?')}",
        f"Pages: {doc.get('total_pages', 0)}",
        f"Chunks: {doc.get('chunk_count', 0)}",
        f"Indexed: {doc.get('ingestion_timestamp', '?')}",
    ]
    if doc.get("document_path"):
        lines.append(f"Path: {doc['document_path']}")
    return "\n".join(lines)


def format_collections(cols: Sequence[Mapping[str, Any]]) -> str:
    """Format collection listing as a count header followed by a table."""
    if not cols:
        return "No collections"
    listing = _Listing(
        "collection",
        [
            ColumnSpec("COLLECTION", 8, fixed=False),
            ColumnSpec("DOCUMENTS", 9, align="right"),
            ColumnSpec("CHUNKS", 6, align="right"),
        ],
    )
    return listing.render(
        [
            [
                c.get("collection", "?"),
                str(c.get("document_count", 0)),
                str(c.get("chunk_count", 0)),
            ]
            for c in cols
        ]
    )


def format_databases(
    databases: Sequence[Mapping[str, Any]],
    current: str = "default",
) -> str:
    """Format database listing as a count header followed by a table."""
    if not databases:
        return "No databases"
    listing = _Listing(
        "database",
        [
            ColumnSpec("DATABASE", 8, fixed=False),
            ColumnSpec("DOCUMENTS", 9, align="right"),
        ],
    )
    return listing.render(
        [
            [
                ("* " + db.get("name", "?"))
                if db.get("name") == current
                else db.get("name", "?"),
                str(db.get("document_count", 0)),
            ]
            for db in databases
        ]
    )


def format_registrations(regs: Sequence[Mapping[str, Any]]) -> str:
    """Format registration listing as a count header followed by a table."""
    if not regs:
        return "No registered directories"
    listing = _Listing(
        "registration",
        [
            ColumnSpec("COLLECTION", 8),
            ColumnSpec("DIRECTORY", 8, fixed=False),
            ColumnSpec("REGISTERED", 10),
        ],
    )
    return listing.render(
        [
            [
                r.get("collection", "?"),
                r.get("directory", "?"),
                (r.get("registered_at") or "?")[:10],
            ]
            for r in regs
        ]
    )
