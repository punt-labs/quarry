---
description: Ingest a URL, directory, or file into your knowledge base. remember = a specific durable fact, ingest = a URL, learn = a distilled lesson that gets retrieval preference.
argument-hint: "<url, directory, or file path>"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

Determine the ingestion method:

1. If it starts with `http://` or `https://`: **URL** (auto-discovers sitemaps)
2. Otherwise it's a local path — a directory or a single file: **Local path**
   (indexed via directory registration + sync; the `ingest` tool itself only
   accepts URLs, so a local file is reached by registering the directory that
   contains it — this indexes the whole directory, not just the one file)

Expand `~` to the user's home directory, and resolve any relative path to an
absolute one against the current working directory, before calling any tool
(the tool sends the path as given, and a relative path resolved on the wrong
side registers the wrong directory).

## Task

Call the appropriate tool(s):

- **URL**: `mcp__quarry-dev__ingest` with `source` set to the argument
- **Local path**: Call `mcp__quarry-dev__register_directory` with
  `directory` set to the absolute path. If the result contains
  `FileNotFoundError` (the daemon rejects both a nonexistent path and an
  existing file this way — there's no way to tell them apart from the error
  alone), retry once with the path's parent directory. Any other error
  (already registered, name conflict, etc.) is not a file-vs-directory
  problem — report it as-is, do not retry. Then call
  `mcp__quarry-dev__sync_all_registrations`

The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
