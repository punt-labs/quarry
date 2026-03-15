---
description: Search your knowledge base
argument-hint: "<query>"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

Use the full arguments text as the search query.

## Task

Call `mcp__plugin_quarry-dev_quarry__find` with `query` set to the arguments.

If the result says "No results", say so briefly.

**Determine the query type:**

- **Question** — the query asks for an answer (starts with who/what/how/why/when/where, ends with `?`, or contains phrases like "is the", "how to", "how do"): provide a concise direct answer based on the search results, citing the source document and page number for each claim. Do not dump the raw results.

- **Keywords** — the query is exploratory (keywords, phrases, topic names): emit the tool output exactly as returned — character for character, including the leading ▶ unicode character. Do not reformat, add commentary, wrap in code fences, convert to markdown tables, or add boxes around the output.
