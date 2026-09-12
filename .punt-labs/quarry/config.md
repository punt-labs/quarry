---
auto_capture:
  session_sync: true
  web_fetch: true
  compaction: true
  session_end: true
  web_search: true
  read: false
  subagent_stop: true
---

# Quarry Project Configuration

This file controls quarry's passive knowledge capture for this project.
Set any field to `false` to disable that capture type.

- `session_sync`: auto-index project files on session start
- `web_fetch`: auto-ingest URLs fetched during research
- `compaction`: capture session transcripts before context compaction
- `session_end`: capture session transcripts on session close (fires even
  when a session never compacts)
- `web_search`: auto-ingest a digest of `WebSearch` results
- `read`: capture prose files (.md/.pdf/.docx/...) read from outside the
  tree — default `false` because `Read` fires often and has the highest
  secret-leak surface; opt in after confirming the filter set is clean
- `subagent_stop`: capture subagent transcripts on completion
