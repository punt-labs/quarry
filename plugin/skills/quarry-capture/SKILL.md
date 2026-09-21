---
name: quarry-capture
description: >
  Persist something worth keeping into quarry so it survives context
  compaction and is findable by `quarry-recall` later — a durable fact, a
  distilled lesson, or a URL to index. Trigger phrases: "remember this",
  "save this fact", "note that we decided X", "learn this lesson", "the rule
  going forward is X", "ingest this URL/page." Covers `quarry remember`
  (one durable fact/procedure/opinion/observation, scoped to an agent
  handle), `quarry learn` (a distilled, project-wide lesson that outranks
  ordinary results), and `quarry ingest` (a URL, sitemap-aware). Use at the
  moment of discovery — a root cause found, a decision ratified, a how-to
  worked out — not as an end-of-session dump. Skip it for progress
  narration, file contents already in the repo, or anything findable by
  grep.
---

# Quarry Capture

Three verbs, three distinct jobs. Pick the one that matches what actually
happened, not the one that's easiest to reach for.

## Verb taxonomy

- **`remember`** — one durable, agent-scoped item: a fact, a procedure, an
  opinion, or an observation. Filed under an `agent_handle`, findable later
  by that handle or by anyone searching broadly.
- **`learn`** — a distilled lesson the whole project should follow (a rule,
  a convention, a "do it this way" insight). No handle — it's project-wide,
  not personal — and it gets retrieval preference over ordinary results.
- **`ingest`** — a URL. Fetches and indexes it (sitemap-aware, single-page
  fallback). For local files or directories already on disk, use
  `register_directory` + `sync`, not `ingest`.

## Recognize the trigger and the five moments

Call `remember` (or `learn`) at these moments — not at the end of a session,
and not never:

1. You found a non-obvious root cause or gotcha → `remember`, `memory_type=fact`.
2. A decision was ratified, with its reason → `remember`, `memory_type=fact`.
3. You worked out a repeatable how-to → `remember`, `memory_type=procedure`.
4. You formed a judgement you'll want to revisit → `remember`, `memory_type=opinion`.
5. Once, before submitting a mission result — one note on what you'd tell
   yourself next time → `remember`, `memory_type=observation`.

Separately: when the insight is a rule the whole team/project should
follow, not just your own note-to-self → `learn` instead of `remember`.

## Run it

```sh
quarry remember "<content>" --document-name <topic-slug> \
  --agent-handle <your-handle> --memory-type fact --summary "<one line>"

quarry learn "<lesson, <=500 chars>" [--topic <domain>] [--name <slug>]

quarry ingest <url> [--overwrite] [--collection <name>]
```

Confirm the exact flags with `quarry remember --help` / `quarry learn
--help` / `quarry ingest --help` before relying on one not shown here.

## The agent_handle discipline

Always pass your **own** handle explicitly on `remember`. The daemon cannot
infer identity, and a sub-agent's working directory resolves to the repo's
leader, not to the sub-agent itself — an omitted or borrowed handle files
the memory under the wrong owner, where its rightful author will never
think to look for it.

## What not to capture

- Progress narration ("now editing file X") — not durable, not useful later.
- File contents or tool output already in the repo or the transcript —
  quarry already captures transcripts; don't duplicate them by hand.
- Anything a `grep` or a file read would answer directly.
- Secrets. The daemon scrubs known secret patterns at write time, but that
  is a backstop, not a reason to paste credentials into a memory.

See `references/memory.md` for the full memory-type vocabulary, the 30-day
decay window, and how lessons outrank other results.
