---
description: Find the original document for a claim or topic
argument-hint: "<claim or text>"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

## Task

Search the knowledge base to identify where specific information comes from.

1. Call `mcp__plugin_quarry-dev_quarry__search_documents` with `query` set to the arguments.

2. From the results, identify the source document(s) and cite them clearly: document name, page number, and the relevant excerpt. Focus on identifying WHERE the information comes from, not explaining it.

3. If no results are found, say so and suggest the user ingest the relevant content first with `/ingest`.
