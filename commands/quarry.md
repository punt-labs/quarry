---
description: Manage your quarry knowledge base
argument-hint: "[status|sync|collections|databases|registrations]"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

Parse the first token as a subcommand. Default to `status` if empty.

## Subcommands

- **status** (default): Call `mcp__plugin_quarry-dev_quarry__status`
- **sync**: Call `mcp__plugin_quarry-dev_quarry__sync_all_registrations`
- **collections**: Call `mcp__plugin_quarry-dev_quarry__list_collections`
- **databases**: Call `mcp__plugin_quarry-dev_quarry__list_databases`
- **registrations**: Call `mcp__plugin_quarry-dev_quarry__list_registrations`

## Output

For `sync`: the result is already formatted by a PostToolUse hook. Do not repeat or reformat. Do not send any text after the tool call.

For all other subcommands: emit the tool output exactly as returned — character for character, including the leading ▶ unicode character. Do not reformat, add commentary, wrap in code fences, convert to markdown tables, or add boxes around the output.
