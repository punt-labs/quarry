---
description: Remember inline text content in your knowledge base. remember = a specific durable fact, ingest = a URL, learn = a distilled lesson that gets retrieval preference.
argument-hint: "<name for this memory>"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

The arguments are the name for this memory, not a filename.

## Task

Ask the user for the content to remember (or accept it from the conversation context if already provided).

Call `mcp__quarry__remember` with:

- `content` set to the text content
- `document_name` set to the arguments (the memory's name)

The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
