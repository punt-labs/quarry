"""Output formatting for MCP tool responses.

Adapts biff's constrained-width table formatter (DES-014) for quarry's
data types.  Data tools return pre-formatted plain text; action tools
return compact summary lines.  The PostToolUse hook routes these to the
UI panel and LLM context.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

# Layout constants ────────────────────────────────────────────────────────────

TABLE_WIDTH = 80
_COL_SEP = "  "
_HEADER_PREFIX = "\u25b6  "  # ▶
_ROW_PREFIX = "   "
_PREFIX_LEN = 3  # len(_HEADER_PREFIX) == len(_ROW_PREFIX)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Column specification ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ColumnSpec:
    """One column in a constrained-width table.

    Exactly one column per table should have ``fixed=False`` — it gets
    the remaining width budget and its content wraps when exceeded.
    """

    header: str
    min_width: int
    fixed: bool = True
    align: Literal["left", "right"] = "left"


# Helpers ─────────────────────────────────────────────────────────────────────


def visible_width(s: str) -> int:
    """Printable width of *s*, ignoring ANSI escapes."""
    return len(_ANSI_RE.sub("", s))


def truncate(text: str, max_len: int = 160) -> str:
    """Truncate *text* to *max_len* chars, collapsing whitespace."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 3].rstrip() + "..."


def _fmt_size(size_bytes: int) -> str:
    """Format bytes as human-readable size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    kib = size_bytes / 1024
    if kib < 1024:
        return f"{kib:.1f} KB"
    mib = kib / 1024
    if mib < 1024:
        return f"{mib:.1f} MB"
    gib = mib / 1024
    return f"{gib:.1f} GB"


def _fmt_cell(text: str, width: int, align: Literal["left", "right"]) -> str:
    """Pad *text* to *width* using visible width (ANSI-aware)."""
    padding = max(0, width - visible_width(text))
    if align == "right":
        return " " * padding + text
    return text + " " * padding


# Table formatter ─────────────────────────────────────────────────────────────


def _render_rows(
    specs: list[ColumnSpec],
    rows: list[list[str]],
    col_widths: list[int],
    var_idx: int | None,
    var_offset: int,
) -> list[str]:
    """Render data rows, wrapping the variable column when needed."""
    n = len(specs)
    output: list[str] = []
    indent = " " * var_offset

    for row in rows:
        if var_idx is None:
            cells = [_fmt_cell(row[i], col_widths[i], specs[i].align) for i in range(n)]
            output.append(_ROW_PREFIX + _COL_SEP.join(cells))
        else:
            chunks = textwrap.wrap(row[var_idx], col_widths[var_idx]) or [""]
            for chunk_i, chunk in enumerate(chunks):
                if chunk_i == 0:
                    cells = [
                        _fmt_cell(
                            chunk if i == var_idx else row[i],
                            col_widths[i],
                            specs[i].align,
                        )
                        for i in range(n)
                    ]
                    output.append(_ROW_PREFIX + _COL_SEP.join(cells))
                else:
                    output.append(indent + chunk)

    return output


def format_table(specs: list[ColumnSpec], rows: list[list[str]]) -> str:
    """Render a constrained-width table with header and data rows.

    Fits within :data:`TABLE_WIDTH` (80) columns.  Fixed columns grow to
    content width.  The single variable column (``fixed=False``) gets the
    remaining budget and wraps on overflow.

    Returns ``▶  HEADER\\n   row\\n   row...`` format.
    """
    n = len(specs)

    # Identify the variable column (at most one).
    var_idx: int | None = None
    for i, spec in enumerate(specs):
        if not spec.fixed:
            if var_idx is not None:
                msg = "format_table: at most one variable column allowed"
                raise ValueError(msg)
            var_idx = i

    # Measure content widths.
    col_widths: list[int] = []
    for i, spec in enumerate(specs):
        content_max = max(
            (visible_width(row[i]) for row in rows),
            default=0,
        )
        col_widths.append(max(spec.min_width, len(spec.header), content_max))

    # Constrain the variable column.
    sep_total = len(_COL_SEP) * (n - 1)
    if var_idx is not None:
        fixed_total = sum(w for i, w in enumerate(col_widths) if i != var_idx)
        budget = TABLE_WIDTH - _PREFIX_LEN - fixed_total - sep_total
        budget = max(specs[var_idx].min_width, budget)
        col_widths[var_idx] = budget

    # Variable column offset for continuation lines.
    var_offset = (
        _PREFIX_LEN + sum(col_widths[:var_idx]) + len(_COL_SEP) * var_idx
        if var_idx is not None
        else 0
    )

    # Header.
    header_cells = [
        _fmt_cell(spec.header, col_widths[i], spec.align)
        for i, spec in enumerate(specs)
    ]
    header = _HEADER_PREFIX + _COL_SEP.join(header_cells)

    # Rows.
    body = _render_rows(specs, rows, col_widths, var_idx, var_offset)
    return "\n".join([header, *body])


# Data tool formatters ────────────────────────────────────────────────────────


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
            excerpt = truncate(text)
            # Indent excerpt lines under the result header
            wrapped = textwrap.wrap(excerpt, TABLE_WIDTH - 6)
            lines.extend(f"      {line}" for line in wrapped)
        lines.append("")

    return "\n".join(lines).rstrip()


def format_documents(docs: Sequence[Mapping[str, Any]]) -> str:
    """Format document listing as a table."""
    if not docs:
        return "No documents"

    specs = [
        ColumnSpec("DOCUMENT", 8, fixed=False),
        ColumnSpec("COLLECTION", 8),
        ColumnSpec("PAGES", 5, align="right"),
        ColumnSpec("CHUNKS", 6, align="right"),
    ]
    rows = [
        [
            d.get("document_name", "?"),
            d.get("collection", "?"),
            str(d.get("total_pages", 0)),
            str(d.get("chunk_count", 0)),
        ]
        for d in docs
    ]
    return format_table(specs, rows)


def format_collections(cols: Sequence[Mapping[str, Any]]) -> str:
    """Format collection listing as a table."""
    if not cols:
        return "No collections"

    specs = [
        ColumnSpec("COLLECTION", 8, fixed=False),
        ColumnSpec("DOCUMENTS", 9, align="right"),
        ColumnSpec("CHUNKS", 6, align="right"),
    ]
    rows = [
        [
            c.get("collection", "?"),
            str(c.get("document_count", 0)),
            str(c.get("chunk_count", 0)),
        ]
        for c in cols
    ]
    return format_table(specs, rows)


def format_databases(
    databases: Sequence[Mapping[str, Any]],
    current: str = "default",
) -> str:
    """Format database listing as a table."""
    if not databases:
        return "No databases"

    specs = [
        ColumnSpec("DATABASE", 8, fixed=False),
        ColumnSpec("DOCUMENTS", 9, align="right"),
        ColumnSpec("SIZE", 8, align="right"),
    ]
    rows = [
        [
            ("* " + db.get("name", "?"))
            if db.get("name") == current
            else db.get("name", "?"),
            str(db.get("document_count", 0)),
            _fmt_size(db.get("size_bytes", 0)),
        ]
        for db in databases
    ]
    return format_table(specs, rows)


def format_registrations(regs: Sequence[Mapping[str, Any]]) -> str:
    """Format registration listing as a table."""
    if not regs:
        return "No registered directories"

    specs = [
        ColumnSpec("COLLECTION", 8),
        ColumnSpec("DIRECTORY", 8, fixed=False),
        ColumnSpec("REGISTERED", 10),
    ]
    rows = [
        [
            r.get("collection", "?"),
            r.get("directory", "?"),
            r.get("registered_at", "?")[:10],
        ]
        for r in regs
    ]
    return format_table(specs, rows)


def format_status(info: Mapping[str, Any]) -> str:
    """Format database status as key-value pairs."""
    lines = [
        "\u25b6  quarry status",
        f"   Documents:      {info.get('document_count', 0)}",
        f"   Collections:    {info.get('collection_count', 0)}",
        f"   Chunks:         {info.get('chunk_count', 0):,}",
        f"   Directories:    {info.get('registered_directories', 0)}",
        f"   Database:       {info.get('database_path', '?')}",
        f"   Size:           {_fmt_size(info.get('database_size_bytes', 0))}",
        f"   Model:          {info.get('embedding_model', '?')}",
    ]
    return "\n".join(lines)


# Action tool formatters (compact summary lines) ─────────────────────────────


def format_ingest_summary(result: Mapping[str, Any]) -> str:
    """One-line summary of a document ingestion."""
    doc = result.get("document_name", "?")
    chunks = result.get("chunks", 0)
    col = result.get("collection", "?")
    return f"\u25b6  Ingested {doc} \u2192 {chunks} chunks in {col}"


def format_sitemap_summary(result: Mapping[str, Any]) -> str:
    """One-line summary of a sitemap crawl."""
    ingested = result.get("ingested", 0)
    skipped = result.get("skipped", 0)
    failed = result.get("failed", 0)
    discovered = result.get("total_discovered", 0)
    after_filter = result.get("after_filter", 0)
    col = result.get("collection", "?")
    parts = [f"{ingested} ingested"]
    if skipped:
        parts.append(f"{skipped} skipped")
    if failed:
        parts.append(f"{failed} failed")
    summary = ", ".join(parts)
    scope = f"{discovered} discovered \u2192 {after_filter} after filter"
    return f"\u25b6  Sitemap {col}: {summary} ({scope})"


def format_sync_summary(results: Mapping[str, Any]) -> str:
    """One-line summary of a sync operation."""
    n = results.get("collections_synced", 0)
    if n == 0:
        return "\u25b6  No collections to sync"
    per_col = results.get("results", {})
    total_ingested = sum(r.get("ingested", 0) for r in per_col.values())
    total_deleted = sum(r.get("deleted", 0) for r in per_col.values())
    total_skipped = sum(r.get("skipped", 0) for r in per_col.values())
    parts = []
    if total_ingested:
        parts.append(f"{total_ingested} ingested")
    if total_deleted:
        parts.append(f"{total_deleted} deleted")
    if total_skipped:
        parts.append(f"{total_skipped} skipped")
    detail = ", ".join(parts) if parts else "no changes"
    return f"\u25b6  Synced {n} collection{'s' if n != 1 else ''}: {detail}"


def format_delete_summary(entity: str, name: str, chunks: int) -> str:
    """One-line summary of a delete operation."""
    plural = "s" if chunks != 1 else ""
    return f"\u25b6  Deleted {entity} {name} ({chunks} chunk{plural})"


def format_register_summary(directory: str, collection: str) -> str:
    """One-line summary of a directory registration."""
    return f"\u25b6  Registered {directory} \u2192 {collection}"


def format_deregister_summary(
    collection: str,
    count: int,
    *,
    data_deleted: bool,
) -> str:
    """One-line summary of a directory deregistration."""
    if data_deleted and count:
        plural = "s" if count != 1 else ""
        suffix = f", {count} doc{plural} removed"
    else:
        suffix = ""
    return f"\u25b6  Deregistered {collection}{suffix}"


def format_switch_summary(previous: str, current: str, path: str) -> str:
    """One-line summary of a database switch."""
    return f"\u25b6  Switched {previous} \u2192 {current} ({path})"
