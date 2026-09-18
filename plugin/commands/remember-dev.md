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

Call `mcp__quarry-dev__remember` with:

- `content` set to the text content
- `document_name` set to the arguments (the memory's name)
- `agent_handle` set to your own handle from the `## Memory` block in your context, when you have one — the daemon cannot infer who you are, and a subagent's working directory resolves to the repo's leader, not to you
- `memory_type` set to one of `fact`, `observation`, `procedure`, `opinion` when the content fits one (`lesson` is reserved for `/learn`); leave it unset otherwise

The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
