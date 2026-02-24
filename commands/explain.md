---
description: Explain a document or topic from your knowledge base
argument-hint: "<document or topic>"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

## Task

Search the knowledge base to find relevant content, then provide a clear explanation.

1. Call `mcp__plugin_quarry-dev_quarry__search_documents` with a query derived from the arguments. If the arguments look like a document name (has a file extension like `.pdf`, `.md`, `.py`), also set `document_filter` to the document name.

2. Using the search results, provide a clear, concise explanation of the document or topic. If results span multiple documents, synthesize across them. Cite specific documents and page numbers in your explanation.

3. If no results are found, say so and suggest the user ingest the relevant content first with `/ingest`.
