---
description: Save a distilled lesson that gets retrieval preference. remember = a specific durable fact, ingest = a URL, learn = a distilled lesson that gets retrieval preference.
argument-hint: "<lesson text> [as <name>]"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

If the arguments end with `" as <name>"`, the text before that clause is the
lesson and `<name>` is the name. Otherwise the full argument string is the
lesson and no name is given.

## Task

Call `mcp__quarry__learn` with:

- `lesson` set to the lesson text
- `name` set to the parsed name, if any

The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
