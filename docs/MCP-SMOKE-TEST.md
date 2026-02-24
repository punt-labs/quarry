# MCP Smoke Test

Manual verification script for quarry's MCP tools inside Claude Code.
Run after release builds, package renames, or MCP server changes.

## Prerequisites

```bash
uv tool install punt-quarry          # or --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ for TestPyPI
quarry install                       # registers MCP server via claude mcp add
quarry doctor                        # all checks pass
```

Verify the MCP server is connected:

```bash
claude mcp list                      # quarry: ... - Connected
```

## 1. Sitemap crawl (ingest_sitemap)

**Call:** `ingest_sitemap` with a known docs site.

```
url: https://code.claude.com/docs/sitemap.xml
collection: smoke-test
include_patterns: /docs/en/*
limit: 5
```

**Verify:**
- `total_discovered` > 0 (sitemap parsed)
- `after_filter` <= `total_discovered` (include filter applied)
- `ingested` == 5 (limit honored)
- `failed` == 0
- `errors` == []

## 2. Dedup on re-crawl (ingest_sitemap)

**Call:** same as step 1, identical parameters.

**Verify:**
- `ingested` == 0 (all skipped via lastmod)
- `skipped` == 5

## 3. Search (search_documents)

**Call:**

```
query: how do MCP servers work
collection: smoke-test
limit: 3
```

**Verify:**
- `total_results` == 3
- Each result has `document_name` starting with `https://code.claude.com/docs/en/`
- Each result has a numeric `similarity` score
- `source_format` == `.html`

## 4. List documents (get_documents)

**Call:**

```
collection: smoke-test
```

**Verify:**
- Returns 5 documents
- Each has `document_name`, `collection`, `chunk_count`, `ingestion_timestamp`

## 5. Single URL ingest (ingest_url)

**Call:**

```
url: https://docs.python.org/3/library/json.html
collection: smoke-test
```

**Verify:**
- `document_name` == `https://docs.python.org/3/library/json.html`
- `chunks` > 0

## 6. Inline content ingest (ingest_content)

**Call:**

```
content: "Quarry is a local semantic search tool. It indexes documents and exposes them to LLMs via MCP."
document_name: smoke-test-inline.md
collection: smoke-test
```

**Verify:**
- `chunks` >= 1

## 7. Search across sources (search_documents)

**Call:**

```
query: JSON encoding and decoding
collection: smoke-test
limit: 3
```

**Verify:**
- Results include the Python json.html doc (proves URL ingest is searchable)

## 8. Get page content (get_page)

**Call:** use a `document_name` and `page_number` from a search result above.

**Verify:**
- Returns full page text (not just the chunk snippet)
- `document_name` and `page_number` match the request

## 9. Delete document (delete_document)

**Call:**

```
document_name: smoke-test-inline.md
collection: smoke-test
```

**Verify:**
- `chunks_deleted` >= 1

## 10. Delete collection (delete_collection)

**Call:**

```
collection: smoke-test
```

**Verify:**
- `chunks_deleted` > 0

## 11. Verify cleanup (list_collections)

**Call:** `list_collections`

**Verify:**
- `smoke-test` no longer appears in the list

## Quick pass criteria

All 11 steps complete with expected results. No `failed > 0`, no unexpected errors.
