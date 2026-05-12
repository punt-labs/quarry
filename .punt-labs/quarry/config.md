---
auto_capture:
  session_sync: true
  web_fetch: true
  compaction: true
---

# Quarry Project Configuration

This file controls quarry's passive knowledge capture for this project.
Set any field to `false` to disable that capture type.

- `session_sync`: auto-index project files on session start
- `web_fetch`: auto-ingest URLs fetched during research
- `compaction`: capture session transcripts before context compaction
