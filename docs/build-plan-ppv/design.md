# Design: Passive Knowledge Capture Hooks

**Beads**: quarry-fon (P1 removal), quarry-ppv (P2 knowledge capture)
**Status**: Draft
**Date**: 2026-03-25

---

## Problem

Quarry's PreToolUse/Bash hook enforces dev workflow conventions: quality gates before committing, `uv` instead of `pip`, git safety reminders. These are dev-time concerns that belong in CLAUDE.md, not in a product that ships to users. A user running quarry in a Go project gets told to run `make check` and `uv`. Several rules duplicate Claude Code's built-in safety behavior.

The README describes quarry's hooks as "passive knowledge capture" — knowledge flows through sessions and the plugin captures it. The pre-tool-hint hook violates this contract. It should be replaced with infrastructure that serves quarry's actual mission: growing the knowledge base.

## Decision

Remove all dev-convention hook infrastructure. Replace the Bash-command accumulator with a knowledge event accumulator that tracks research activity and enriches session captures.

Two PRs. PR 1 is pure deletion (quarry-fon). PR 2 adds the knowledge accumulator (quarry-ppv).

---

## PR 1: Remove dev conventions (quarry-fon)

### What goes

| Component | File | Why |
|-----------|------|-----|
| Instant rules (git-add, pip, force-push, no-verify) | `src/quarry/hint_rules.py` | Dev conventions, some duplicate Claude Code built-ins |
| Sequence rules (make check, solo gate) | `src/quarry/hint_rules.py` | Dev workflow enforcement |
| Bash-command accumulator | `src/quarry/hint_accumulator.py` | Built for wrong purpose — tracks Bash commands, not knowledge events |
| PreToolUse/Bash hook entry | `hooks/hooks.json` | No longer needed |
| Hook handler | `src/quarry/hooks.py` `handle_pre_tool_hint` | Dead code |
| Hook entry point | `src/quarry/_hook_entry.py` | Dead code |
| CLI dispatch | `src/quarry/__main__.py` | Dead code |
| Config field | `src/quarry/_stdlib.py` `convention_hints` in `HookConfig` | Dead config |
| Shell wrapper | `hooks/pre-tool-hint.sh` | Dead code |
| All tests | `tests/test_hint_rules.py`, `tests/test_hint_accumulator.py`, hint tests in `tests/test_hooks.py` | Testing removed code |

### What stays

Three hooks remain, all serving knowledge capture:

1. **SessionStart** — auto-registers project directory
2. **PostToolUse/WebFetch** — auto-ingests fetched URLs
3. **PreCompact** — captures conversation transcript before compaction

### Documentation changes

- README line 161: "Three hooks" not four. Remove convention hints bullet (line 166) and roadmap row (line 185).
- README line 168: Remove `convention_hints: false` config example.
- prfaq.tex FAQ and feature appendix: remove "convention hints" / "accumulator" language.
- CHANGELOG: removal entry.

---

## PR 2: Knowledge event accumulator (quarry-ppv)

### Design

The accumulator tracks knowledge-generating events within a session. It replaces the Bash-command accumulator with a different shape optimized for quarry's mission.

#### KnowledgeEvent

```python
@dataclass(frozen=True)
class KnowledgeEvent:
    ts: float
    kind: str              # "web_fetch" (PR 2), "read" and "search" (future)
    source: str            # URL for web_fetch
    collection: str | None # quarry collection if known; None for web captures
```

`collection` is `str | None` because web fetches don't belong to a project collection — they go into `web-captures`.

PR 2 tracks only `web_fetch` events (the PostToolUse/WebFetch hook already exists). Future event kinds:

| Kind | Hook | Roadmap |
|------|------|---------|
| `web_fetch` | PostToolUse/WebFetch | PR 2 |
| `read` | PostToolUse/Read (new hook entry) | `quarry learn all` |
| `search` | PostToolUse on quarry MCP tools | `quarry learn all` |

#### KnowledgeAccumulator

Same rolling-window architecture as the old accumulator:

- **TTL**: 86400s (24h). Events survive the whole session, not just 5 minutes.
- **max_events**: 200. Higher ceiling — sessions can involve dozens of URLs.
- **State file**: `/tmp/quarry-knowledge-{safe_session_id}.json`. Volatile, session-scoped. Same `_sanitize_session_id` pattern as old accumulator. No need to survive crashes — if state is lost, PreCompact simply produces a transcript without the research summary.
- **Clock**: injectable for deterministic testing.
- **Deserialization**: fail-open (corrupt JSON returns empty accumulator).

#### PostToolUse/WebFetch integration

The existing `handle_post_web_fetch` auto-ingests URLs. Enhancement:

```
1. Extract URL from payload (_extract_url)
2. If URL valid:
   a. Record KnowledgeEvent(kind="web_fetch", source=url)  <-- NEW
   b. Check dedup (already ingested?)
   c. If new: ingest content
```

The event records on every valid URL extraction, before the dedup check. This tracks "URLs Claude researched this session" not "URLs quarry ingested." A URL fetched twice in one session is still one research event (dedup in the accumulator by source).

The `web_fetch` config toggle gates both ingestion AND event recording. If `web_fetch: false`, no events are recorded.

#### PreCompact enrichment

The existing `handle_pre_compact` captures conversation transcripts. Enhancement:

```
1. Read transcript (existing)
2. Read knowledge accumulator for this session  <-- NEW
3. If events exist: prepend research summary    <-- NEW
4. Ingest as document (existing)
```

Research summary format, prepended to transcript text:

```markdown
## Research This Session

URLs fetched:
- https://example.com/api-docs
- https://other.example.com/reference

---

[transcript follows]
```

If the accumulator is missing, empty, or unreadable: no summary prepended. Fail-open. The transcript is captured regardless.

#### Session ID consistency

The same `session_id` from the payload must be used in PostToolUse/WebFetch (write) and PreCompact (read). Both receive it from Claude Code's hook system. A test must verify the same session ID across both handlers produces the expected enrichment.

### Test plan

| Test | Validates |
|------|-----------|
| KnowledgeAccumulator round-trip (JSON serialize/deserialize) | Data integrity |
| KnowledgeAccumulator TTL expiry (24h) | Events survive session, expire eventually |
| KnowledgeAccumulator overflow (>200 events) | Oldest events pruned |
| KnowledgeAccumulator corrupt JSON | Fail-open, returns empty |
| KnowledgeEvent with collection=None | Optional field works |
| PostToolUse/WebFetch records event on valid URL | Event recording |
| PostToolUse/WebFetch records event even on dedup path | Research tracking vs ingestion tracking |
| PostToolUse/WebFetch respects web_fetch config toggle | Config gating |
| PreCompact includes research summary when events exist | Enrichment |
| PreCompact works unchanged when no events | Fail-open |
| Same session_id across PostToolUse and PreCompact | Integration |

### Documentation changes

- README: describe research tracking in PostToolUse/WebFetch description, update PreCompact to mention research context.
- CHANGELOG: addition entry.

---

## Hooks after both PRs

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": ".../session-start.sh" }] },
      { "hooks": [{ "type": "command", "command": ".../session-sync.sh" }] }
    ],
    "PostToolUse": [
      { "matcher": "mcp__...quarry...", "hooks": [{ "type": "command", "command": ".../suppress-output.sh" }] },
      { "matcher": "WebFetch", "hooks": [{ "type": "command", "command": ".../web-fetch.sh" }] }
    ],
    "PreCompact": [
      { "hooks": [{ "type": "command", "command": ".../pre-compact.sh", "timeout": 30000 }] }
    ]
  }
}
```

No PreToolUse block. Three hooks, all serving knowledge capture.

---

## Rejected alternatives

| Alternative | Why rejected |
|-------------|-------------|
| Keep instant rules as "universal safety" | They duplicate Claude Code built-ins. Quarry is not a safety tool. |
| Replace sequence rules with `make check` | Still dev conventions. Language-agnostic is better but still wrong. |
| Track all Read events in PR 2 | Needs a new PostToolUse/Read hook entry. High noise. Defer to `quarry learn all`. |
| Persist accumulator to `~/.quarry/` | Overkill for session-scoped volatile data. /tmp is the right place. |
| Record events only on fresh ingestion | Misses re-fetched URLs. Research tracking should track research, not ingestion. |
| Combine both PRs | Reviewers must track removal and addition simultaneously. Split is cleaner. |
