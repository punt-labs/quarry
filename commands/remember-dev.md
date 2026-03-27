---
description: Remember inline text content in your knowledge base
argument-hint: "<document name>"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

The arguments should be the document name for the content being remembered.

## Task

Ask the user for the content to remember (or accept it from the conversation context if already provided).

Call `mcp__plugin_quarry-dev_quarry__remember` with:

- `content` set to the text content
- `document_name` set to the arguments (document name)

The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
