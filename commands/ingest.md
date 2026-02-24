---
description: Ingest a URL, directory, or file into your knowledge base
argument-hint: "<url, directory, or file path>"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

Determine the ingestion method:

1. If it starts with `http://` or `https://`: **URL** (auto-discovers sitemaps)
2. If it's a local directory path (ends with `/` or is a known directory): **Directory** (register + sync)
3. Otherwise: **File** (single file ingestion)

Expand `~` to the user's home directory before calling any tool.

## Task

Call the appropriate tool(s):

- **URL**: `mcp__plugin_quarry_quarry__ingest_auto` with `url` set to the argument
- **Directory**: Call `mcp__plugin_quarry_quarry__register_directory` with `directory` set to the absolute path, then call `mcp__plugin_quarry_quarry__sync_all_registrations`
- **File**: `mcp__plugin_quarry_quarry__ingest_file` with `file_path` set to the argument

The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
