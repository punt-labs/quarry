# SageOx Competitive Analysis

**Date:** 2026-02-28
**Source:** https://sageox.ai
**Purpose:** Understand where quarry sits relative to SageOx in the agent memory space

## What SageOx Does

SageOx is a hosted context infrastructure platform for human-agent collaboration. It captures team discussions and agent coding sessions as "durable team artifacts" that persist across projects and sessions.

Four-stage pipeline:

1. **Capture** — Record technical discussions and agent coding sessions
2. **Structure** — Extract summaries, decisions, and intent into searchable formats
3. **Consult** — Surface relevant context to teams and new agent sessions
4. **Ship** — Link code commits back to the decisions that motivated them

Key components:

- **Team Context** — Defines principles and constraints that agents inherit
- **Ledger of Work** — Documents reasoning and intent alongside code
- **Ox CLI** — Primes agent sessions with shared context
- **Web Application** — Team inspection and navigation of shared history

Integrates with Cursor, GitHub Copilot, and Claude Code. Built with Next.js. No public pricing.

## Problem Validation

SageOx validates the core problem quarry's ambient knowledge vision addresses: **knowledge evaporates between agent sessions.** Their framing:

- "Decisions fragment" across sessions
- "PR origins lack context" — code exists but the reasoning is gone
- "Humans repeatedly explain intent" — the same context re-entered every session
- "Architecture slowly drifts" — without shared memory, consistency degrades

This is the same problem quarry's learning and recall loops solve. The market agrees this is real.

## Where Quarry and SageOx Overlap

| Capability | SageOx | Quarry (with ambient vision) |
|-----------|--------|------------------------------|
| Capture agent sessions | Yes — records sessions as artifacts | Yes — PreCompact hook captures transcripts |
| Capture web research | Not mentioned | Yes — PostToolUse on WebFetch/WebSearch |
| Prime new sessions with context | Yes — Ox CLI injects shared context | Yes — SessionStart briefing hook |
| Semantic search over history | Yes — structured artifacts are searchable | Yes — LanceDB vector search over all captured content |
| Link code to decisions | Yes — commits linked to decision ledger | No — quarry tracks knowledge, not provenance |

## Where They Differ

**Hosted vs. local.** SageOx is a cloud platform with a web app. Quarry runs entirely on your machine. No accounts, no API keys, no data leaving your laptop.

**Structured vs. raw.** SageOx processes captures into structured formats (summaries, decisions, intent). Quarry indexes raw content and lets semantic search surface what's relevant. Structured extraction is more precise but lossy. Raw indexing preserves everything but requires better search.

**Team vs. individual.** SageOx is built for team collaboration — shared context, web app for inspection, team principles. Quarry is built for one person (or a small team sharing a git repo). There is no multi-user access layer, no web dashboard, no role-based permissions.

**Enterprise vs. open source.** SageOx is a commercial product targeting teams that "build primarily through prompts." Quarry is MIT-licensed, free, and designed for developers who want to own their data.

**Code provenance.** SageOx explicitly links commits to the decisions that produced them. Quarry's session notes capture the same reasoning — adding commit-hash tags would close the gap. The raw material is the same; the difference is how much structure is imposed on it.

## Quarry's Position

Quarry is not an enterprise solution. It is an open source tool for individuals and small teams who want ambient knowledge without a hosted platform.

The competitive advantage is simplicity and locality:

- **No accounts.** Install and use. The knowledge base is a directory on your machine.
- **No cloud dependency.** Embedding model runs locally. Data stays local.
- **No vendor lock-in.** LanceDB is open source. The data is yours.
- **Composable.** Works with Claude Desktop, Claude Code, the CLI, and the menu bar app — same database, different interfaces.

The competitive disadvantage is collaboration. Today, quarry has no mechanism for two people to share a knowledge base. A team of five debugging the same system can't share what they've each learned.

## Potential: Git-Based Team Sharing

Quarry could support small-team sharing by storing raw captured content (markdown files, URL lists) in a git repository. Each team member's quarry instance would vectorize the content locally using their own embedding model. The flow:

```
Developer A captures web research → markdown written to shared repo
Developer A commits and pushes
Developer B pulls → quarry sync picks up new files → vectorized locally
Developer B searches → finds Developer A's research
```

This works because:

- Git already handles sync, conflict resolution, and history
- Raw content (markdown, text) is git-friendly — small diffs, mergeable
- Vectorization is local — no shared embedding infrastructure needed
- Each person's quarry database is independent — different machines, same content

This would not scale to large teams (git repos with thousands of markdown files get unwieldy) but would work well for 2-5 person teams. It is the simplest possible collaboration model — no server, no accounts, no platform.

This is not on the roadmap. It is noted here as a potential direction if team sharing becomes a priority.

## What the Product Actually Is

SageOx's [ox CLI](https://github.com/sageox/ox) is open source (Go). Looking at a real PR ([sageox/ox#4](https://github.com/sageox/ox/pull/4)), the product surface is two links in the PR body:

- **Discussion:** `sageox.ai/team/.../recordings/rec_...` — a recording of the conversation that led to the change
- **Session:** `sageox.ai/repo/.../sessions/2026-02-17.../view` — the agent session that produced the code

That's the core mechanism. Record the conversation, record the session, link them in the PR. The hosted web UI lets the team browse and inspect these artifacts, but the capture itself is just logging.

For quarry, the equivalent is: PreCompact hook captures the conversation transcript, tags it with commit hashes from the session, session note gets indexed. `quarry search "why did we replace flock with socket ping"` returns the answer. No hosted platform, no web UI. Just a searchable local document that got created automatically.

## Provenance Is Not Hard

The sharp distinction between "knowledge tools" and "provenance tools" is overstated. Provenance is just: save the conversation, save the commit, link them.

Quarry's ambient learning hooks already capture conversation transcripts (PreCompact). Git already tracks commits. If the session note includes the commit hashes from that session — or the commit messages reference the session — you have provenance. The transcript contains the reasoning. The commit contains the code change. The link is a tag.

Entire.io built a product around this. SageOx built a platform. The underlying mechanism is logging with correlation. Quarry's learning loop produces the raw material; adding commit-hash tags to session notes would close the provenance loop with minimal additional work.

## Relationship to Other Tools

| Tool | Audience | Model | Differentiator |
|------|----------|-------|----------------|
| SageOx | Enterprise teams | Hosted platform | Structured team context, web UI, decision ledger |
| Entire.io | Individual developers | Local + shadow branch | Deep Claude Code integration, checkpoint/rewind |
| Quarry | Individuals, small teams | Local-first, open source | Semantic search over all captured content, 30+ format support |

All three solve the same core problem: knowledge evaporates between agent sessions. SageOx solves it for enterprises with a hosted platform. Entire.io solves it for individual developers with git-native session recording. Quarry solves it for individuals and small teams with local semantic search — and because the captured content is searchable by meaning (not just keyword), it compounds in value as the knowledge base grows.

The tools are more alike than different. The real differentiators are deployment model (hosted vs. local), audience (enterprise vs. individual), and what you can do with the captured content (structured decisions vs. semantic search).
