---
description: Search your knowledge base
argument-hint: "<query>"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

Use the full arguments text as the search query.

## Task

Call `mcp__plugin_quarry-dev_quarry__search_documents` with `query` set to the arguments.

If the result says "No results", do not emit any text.

Otherwise, emit the tool output exactly as returned — character for character, including the leading ▶ unicode character. Do not reformat, add commentary, wrap in code fences, convert to markdown tables, or add boxes around the output.
