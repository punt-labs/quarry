---
description: Manage your quarry knowledge base
argument-hint: "[status|sync|collections|databases|registrations]"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

Parse the first token as a subcommand. Default to `status` if empty.

## Subcommands

- **status** (default): Call `mcp__plugin_quarry_quarry__status`
- **sync**: Run `quarry sync` via the Bash tool with `run_in_background: true`. This avoids blocking the conversation during long syncs. You will be notified when it completes.
- **collections**: Call `mcp__plugin_quarry_quarry__list_collections`
- **databases**: Call `mcp__plugin_quarry_quarry__list_databases`
- **registrations**: Call `mcp__plugin_quarry_quarry__list_registrations`

## Output

For `sync`: after launching the background command, briefly confirm it's running (e.g., "Syncing in background — you'll be notified when done."). Do not block or poll.

For all other subcommands: emit the tool output exactly as returned — character for character, including the leading ▶ unicode character. Do not reformat, add commentary, wrap in code fences, convert to markdown tables, or add boxes around the output.
