# Post-Release Smoke Test

Manual verification script for quarry after release, install, or MCP
server changes. Exercises every MCP tool and key CLI commands to confirm
the system works end-to-end.

Run from a Claude Code session in any quarry-registered project directory.

## Prerequisites

Daemon running, plugin installed, doctor clean:

```bash
quarry doctor       # all checks pass, including FTS index: healthy
```

Expected: Python, data directory, OCR, embedding model, ONNX provider,
core imports, mcp-proxy, Claude Code MCP, storage, FTS index, sync, and
sync directories all pass. No timeouts.

## Phase 1: MCP Tools

### 1.1 Status

**Tool:** `status`

**Verify:**

- Documents, collections, chunks counts are non-zero
- Provider shows expected backend (CUDAExecutionProvider on GPU hosts)
- Size is reported

### 1.2 Remember (write path)

**Tool:** `remember`

```yaml
content: "Smoke test document for post-release verification. The keyword
  platypus-smoke-token exists only here and should be findable via BM25
  exact match."
document_name: smoke-test-verification.md
collection: smoke-test
summary: Post-release verification document
```

**Verify:** Returns "Remembering ... (background)". Wait 3 seconds for
background ingest.

### 1.3 Find — semantic query

**Tool:** `find`

```yaml
query: post-release verification document
collection: smoke-test
limit: 3
```

**Verify:** `smoke-test-verification.md` appears in results.

### 1.4 Find — BM25 keyword match (hybrid search verification)

**Tool:** `find`

```yaml
query: platypus-smoke-token
collection: smoke-test
limit: 3
```

**Verify:** `smoke-test-verification.md` appears in results. This
keyword has zero semantic similarity to anything — it can only be
found via BM25 full-text search. If this returns no match, hybrid
search is broken (FTS index stale after optimize).

### 1.5 Show — document metadata

**Tool:** `show`

```yaml
document_name: smoke-test-verification.md
collection: smoke-test
```

**Verify:** Shows document name, collection, page count (1), chunk
count (1), and ingestion timestamp.

### 1.6 Show — page text

**Tool:** `show`

```yaml
document_name: smoke-test-verification.md
collection: smoke-test
page_number: 1
```

**Verify:** Returns full text including "platypus-smoke-token".

### 1.7 List documents

**Tool:** `list`

```yaml
kind: documents
collection: smoke-test
```

**Verify:** Shows `smoke-test-verification.md` with chunk count and
timestamp.

### 1.8 List collections

**Tool:** `list`

```yaml
kind: collections
```

**Verify:** `smoke-test` appears in the list with 1 document.

### 1.9 List registrations

**Tool:** `list`

```yaml
kind: registrations
```

**Verify:** Returns registered directories with collection names and
dates. Count matches `quarry doctor` sync directories count.

### 1.10 List databases

**Tool:** `list`

```yaml
kind: databases
```

**Verify:** Shows at least `default` database with document count and
size. Completes in <3 seconds (not 30s — verifies du-based size calc).

### 1.11 Ingest URL

**Tool:** `ingest`

```yaml
source: https://docs.python.org/3/library/json.html
collection: smoke-test
```

**Verify:** Returns "Ingesting ... (background)". Wait 5 seconds, then
search for "JSON encoder decoder" in smoke-test collection — the
Python docs page should appear.

### 1.12 Delete document

**Tool:** `delete`

```yaml
name: smoke-test-verification.md
kind: document
collection: smoke-test
```

**Verify:** Returns "Deleting document ...".

### 1.13 Delete collection (cleanup)

**Tool:** `delete`

```yaml
name: smoke-test
kind: collection
```

**Verify:** Returns "Deleting collection ...". Wait 2 seconds (async),
then confirm with `list(kind="collections")` — `smoke-test` should
be gone.

### 1.14 Use database

**Tool:** `use`

```yaml
name: default
```

**Verify:** Returns confirmation of active database. (Only test if
multiple databases exist; otherwise verify it doesn't error on the
current database.)

### 1.15 Learn (lessons write path)

**Tool:** `learn`

```yaml
lesson: "Smoke lesson: the keyword platypus-lesson-token marks the
  post-release smoke lesson and nothing else."
name: smoke-lesson
```

**Verify:** Returns "▶  Learning saved (accepted, task ...)". Wait 3
seconds, then `find` with query `platypus-lesson-token`: a document named
`lesson-smoke-lesson-<8 hex>` appears, in the `<collection>-lessons`
collection of the registered project (`default-lessons` when the session
runs outside a registered directory), with `memory_type` `lesson`.

Cleanup: `delete` that document (kind `document`, its collection).

### 1.16 Missions sync (dry run)

**Tool:** `missions_sync`

```yaml
dry_run: true
```

**Verify:** Returns one line, `▶  Mission memories: would file N, skipped N,
errors 0`, and posts nothing. A repo with no `.punt-labs/ethos/missions/`
tree reports all zeros — that is a PASS; a non-zero `errors` count is a
FAIL (each error is one line naming the file or mission id).

## Phase 2: CLI Commands

1:1 mirror of Phase 1 using CLI equivalents, plus CLI-only checks.
Run from a terminal on the host where quarry is installed. Uses its
own `smoke-test` collection — run Phase 2 independently (it creates
and cleans up its own data).

### 2.1 Doctor

```bash
quarry doctor
```

**Verify all checks pass:**

- Python version, data directory, local OCR, embedding model
- ONNX provider (CUDA on GPU hosts, CPU on others)
- Core imports, mcp-proxy
- Claude Code MCP: configured (not "timed out")
- Storage size reported
- FTS index: healthy (not "stale" or "missing")
- Sync: N collections, oldest sync Xh ago (not ">24h stale")
- Sync directories: N directories OK (no missing)

### 2.2 Status

```bash
quarry status
```

**Verify:** Documents, chunks, collections, storage reported.

### 2.3 Remember (write path)

```bash
echo "CLI smoke test document. The keyword platypus-cli-token exists only here." \
  | quarry remember --name cli-smoke-test.md --collection smoke-test
```

**Verify:** Reports chunks indexed. Wait 3 seconds.

### 2.4 Find — semantic query

```bash
quarry find "CLI smoke test document" --collection smoke-test
```

**Verify:** `cli-smoke-test.md` appears in results.

### 2.5 Find — BM25 keyword match (hybrid search canary)

```bash
quarry find "platypus-cli-token" --collection smoke-test
```

**Verify:** `cli-smoke-test.md` appears in results. Pure keyword
match — only reachable via BM25.

### 2.6 Show — document metadata

```bash
quarry show cli-smoke-test.md --collection smoke-test
```

**Verify:** Shows document name, collection, pages, chunks, timestamp.

### 2.7 Show — page text

```bash
quarry show cli-smoke-test.md --collection smoke-test --page 1
```

**Verify:** Returns full text including "platypus-cli-token".

### 2.8 List documents

```bash
quarry list documents --collection smoke-test
```

**Verify:** Shows `cli-smoke-test.md` with chunk count.

### 2.9 List collections

```bash
quarry list collections
```

**Verify:** `smoke-test` appears with 1 document.

### 2.10 List registrations

```bash
quarry list registrations
```

**Verify:** Returns registered directories with dates.

### 2.11 List databases

```bash
time quarry list databases
```

**Verify:** Shows `default` database with count and size. Completes
in <3 seconds (du-based, not 30s rglob).

### 2.12 Ingest URL

```bash
quarry ingest https://docs.python.org/3/library/json.html --collection smoke-test
```

**Verify:** Reports chunks indexed. Wait 5 seconds, then:

```bash
quarry find "JSON encoder decoder" --collection smoke-test
```

**Verify:** Python docs page appears in results.

### 2.13 Delete document

```bash
quarry delete cli-smoke-test.md --collection smoke-test
```

**Verify:** Reports a task acceptance (`task_id` + `status`) — delete is
fire-and-forget through the daemon (DES-001), not a synchronous chunk count.

### 2.14 Delete collection (cleanup)

```bash
quarry delete smoke-test --type collection
```

**Verify:** Reports a task acceptance (fire-and-forget). Confirm with
`quarry list collections` — `smoke-test` should be gone.

### 2.15 Use database

```bash
quarry use default
```

**Verify:** Confirms active database. No error.

### 2.16 Version

```bash
quarry version
```

**Verify:** Matches the released version.

### 2.17 Remote (if --network installed)

```bash
quarry remote list --ping
```

**Verify:** Shows remote config with host, port, pinned fingerprint,
and connection status (healthy/unhealthy).

### 2.18 Capture PII redaction (DES-036)

Verifies the write-time scrub that the capture writers (PreCompact,
backfill, WebFetch auto-capture) run before content reaches a
`<name>-captures`/`web-captures` collection.

```bash
# Canary: the shipped scrub actually redacts (this is what CaptureWriter runs).
# Run from a source checkout / the project venv.
uv run python -c "from quarry.scrub import scrub, ScrubConfig; \
o,_=scrub('mail a@b.com path /Users/dave/secret file:///Users/erin/k host testbox', \
ScrubConfig(local_hostname='testbox')); print(o)"

# Scope-boundary negative control: deliberate ingest is NOT scrubbed.
echo "control a@b.com /Users/dave/secret" | quarry remember --name scope --collection smoke-scope
quarry find "control" --collection smoke-scope
```

**Verify:**

- Canary output redacts every class: `[REDACTED:email]`, `~/secret`,
  `file://~/k`, `[REDACTED:hostname]` — and no raw `a@b.com`, `/Users/`,
  `dave`, `erin`, or `testbox` survives.
- The `find` result still shows the raw email and path — deliberate ingest is
  intentionally not scrubbed (only automatic captures are). If the stored
  `remember` text is redacted, the scope boundary is broken (fail).
- (If a real session transcript is handy, `quarry backfill-sessions --dry-run`
  and inspect a written `<name>-captures` `.md` for the same redaction.)

Cleanup: `quarry delete smoke-scope --type collection`.

### 2.19 Learn (lessons write path)

```bash
quarry learn "CLI smoke lesson: platypus-cli-lesson-token marks this lesson." --name cli-smoke-lesson
```

**Verify:** Reports a task acceptance. Wait 3 seconds, then:

```bash
quarry find "platypus-cli-lesson-token"
```

**Verify:** A `lesson-cli-smoke-lesson-<8 hex>` document appears (collection
`<collection>-lessons` for the registered project, else `default-lessons`),
tagged `memory_type` `lesson`. Cleanup:
`quarry delete lesson-cli-smoke-lesson-<hex> --collection <that collection>`.

### 2.20 Missions sync (dry run, DES-055)

```bash
quarry missions sync --dry-run
```

**Verify:** Exit 0 and one line, `▶  Mission memories: would file N,
skipped N, errors 0`. Nothing is posted. All zeros is a PASS when the repo
has no `.punt-labs/ethos/missions/` tree; any non-zero `errors` (exit 1)
is a FAIL. Run it from a repo with closed missions to see `would file`
count the frozen rounds not yet held by the daemon.

### 2.21 Unknown memory type rejected (DES-055)

```bash
echo "smoke probe: this text must never be stored" \
  | quarry remember --name smoke-bad-type --memory-type bogus
echo "exit=$?"
quarry find "smoke probe must never be stored"
```

**Verify:**

- The `remember` exits 1 and prints exactly
  `Error: unknown memory_type 'bogus'; expected one of fact, observation, opinion, procedure`
  (the daemon's 400 body, identical on `remember`, `ingest`, and the capture
  route).
- The `find` returns no `smoke-bad-type` document — the row was rejected,
  not stored with a bad tag. A stored row is a FAIL (the vocabulary gate is
  broken).

No cleanup: nothing was written.

## Phase 3: Enable/Disable

Tests `quarry enable` and `quarry disable` end-to-end. Use a
temporary directory to avoid modifying real project state.

### 3.1 Enable

```bash
mkdir -p /tmp/quarry-smoke-enable
quarry enable /tmp/quarry-smoke-enable
```

**Verify:**

- Exit code 0
- Output shows collection name (derived from directory basename)
- Output shows captures collection (`<name>-captures`)
- `quarry list registrations` includes the new directory
- `.punt-labs/quarry/config.md` exists in the target directory

### 3.2 Enable with custom collection

```bash
mkdir -p /tmp/quarry-smoke-custom
quarry enable /tmp/quarry-smoke-custom --collection custom-smoke
```

**Verify:**

- Output shows collection `custom-smoke`
- Output shows captures `custom-smoke-captures`

### 3.3 Enable idempotent

```bash
quarry enable /tmp/quarry-smoke-enable
```

**Verify:** Exit code 0, `created_registration` is false (reuse).

### 3.4 Doctor enable status

```bash
cd /tmp/quarry-smoke-enable && quarry doctor | grep "Enable status"
```

**Verify:** Shows collection name and "config.md" present.

### 3.5 Disable

```bash
quarry disable /tmp/quarry-smoke-enable
```

**Verify:**

- Exit code 0
- `quarry list registrations` no longer includes the directory
- `.punt-labs/quarry/config.md` removed from target directory

### 3.6 Disable with --keep-data

```bash
quarry disable /tmp/quarry-smoke-custom --keep-data
```

**Verify:** Registration removed but no "Deleted N chunks" message.

### 3.7 Cleanup

```bash
rm -rf /tmp/quarry-smoke-enable /tmp/quarry-smoke-custom
```

## Phase 4: Install Verification

Only run after a fresh install or re-install.

### 4.1 Service unit

```bash
systemctl --user status quarry    # Linux
launchctl list | grep quarry      # macOS
```

**Verify:**

- Service is active/running
- ExecStart points to the `quarryd` engine binary `~/.local/bin/quarryd`
  (NOT `quarry serve`, which is retired, and NOT `.venv/bin/python3`)
- Includes `--host 0.0.0.0` if installed with `--network`
- Includes `--tls`

### 4.2 GPU (NVIDIA hosts only)

```bash
quarry doctor | grep "ONNX provider"
```

**Verify:** Shows `CUDAExecutionProvider (onnx/model_fp16.onnx)`, not
`CPUExecutionProvider`.

### 4.3 Port binding

```bash
ss -tlnp | grep 8420    # Linux
lsof -i :8420            # macOS
```

**Verify:** Listening on `0.0.0.0:8420` (if `--network`) or
`127.0.0.1:8420` (default).

### 4.4 TLS

```bash
curl --cacert ~/.punt-labs/quarry/tls/ca.crt https://localhost:8420/health
```

**Verify:** Returns `{"status":"ok"}` or similar. No TLS errors.

## Phase 5: Agent-Memory Write Loop (DES-055)

Verifies the `SubagentStop` distillation live: a subagent's own final
report lands in `memory-<handle>` as an `observation`, alongside its raw
transcript in `<repo>-captures`. Run from a Claude Code session in a repo
whose vendored registry (`.punt-labs/ethos/identities/<handle>.yaml`)
carries the identity you spawn — the quarry repo itself, with `rmh`, is
the canonical choice.

### 5.1 SubagentStop distillation

In the session, spawn a registered identity with a trivial task, e.g.
`Agent(subagent_type="rmh", prompt="Reply with exactly one line: platypus-subagent-token smoke report.")`,
and wait for it to stop. Then, from a terminal:

```bash
quarry list documents --collection memory-rmh
quarry find "platypus-subagent-token" --agent-handle rmh --memory-type observation
quarry list documents --collection <repo>-captures
```

**Verify:**

- `memory-rmh` holds a new `subagent-<id8>-report` document whose text
  includes `platypus-subagent-token`; the filtered `find` (handle `rmh`,
  type `observation`) returns it, which proves both columns were set.
- `<repo>-captures` holds the raw transcript as `session-<id8>` with the
  **same** eight-character id — one event, two rows.
- Negative control: a bare `Agent(subagent_type="general-purpose", ...)`
  yields the `<repo>-captures` row only; no `memory-*` collection gains a
  `subagent-*-report`, and nothing is filed under the leader's handle.

Cleanup: `quarry delete subagent-<id8>-report --collection memory-rmh` (and
the `session-<id8>` capture if you want the collection clean).

## Quick Pass Criteria

- Phase 1: all 16 MCP tool calls succeed, BM25 keyword match works
  (1.4), cleanup leaves no smoke-test data
- Phase 2: all 21 CLI checks succeed, BM25 keyword match works (2.5),
  the unknown memory type is rejected with exit 1 (2.21), cleanup leaves
  no smoke-test data, `list databases` completes in <3s
- Phase 3: all 7 enable/disable checks succeed, registrations created
  and removed correctly, config.md managed, doctor reports enable status
- Phase 4: service unit points at tool venv, correct bind address,
  CUDA on GPU hosts
- Phase 5: the subagent's report reaches `memory-<handle>` as an
  `observation` and the unattributed control reaches captures only

## Quick Fail Indicators

- `find` returns results but BM25 keyword test (1.4/2.5) fails — FTS
  index stale, `optimize_table` not rebuilding index
- `doctor` shows "claude CLI timed out" — still using subprocess probe
  instead of file-based check
- `doctor` shows "FTS index: stale" — need `quarry sync` to trigger
  rebuild
- `list databases` takes >5 seconds — still using rglob instead of du
- Service ExecStart contains `.venv/bin/python3` or `quarry serve` — a dev
  venv or the retired subcommand baked into the unit; it must exec `quarryd`,
  or it will crash-loop on next restart
- ONNX provider shows CPU on a GPU host — onnxruntime-gpu not installed,
  check install.sh GPU swap
- `quarry enable` crashes on child of registered parent — walk-up
  matching or descendant guard broken
- `remember --memory-type bogus` exits 0 or the row shows up in `find` —
  the server-side `MemoryType` gate is not on that route
- `subagent-<id8>-report` lands in the leader's `memory-*` instead of the
  subagent's — attribution fell back to the repo pin instead of
  `agent_type`
- A `general-purpose` subagent produces a `memory-*` row — non-identities
  must be filed unattributed

## Report Format

After running the smoke test, produce a report in this format. Include
it in the release recap email.

```text
Quarry Smoke Test Report
========================
Version:  <version>
Host:     <hostname>
Date:     <YYYY-MM-DD HH:MM>
Provider: <CUDAExecutionProvider / CPUExecutionProvider>
Mode:     <default / --network>

Phase 1: MCP Tools
  1.1  status                    PASS / FAIL  <notes if fail>
  1.2  remember                  PASS / FAIL
  1.3  find (semantic)           PASS / FAIL
  1.4  find (BM25 keyword)       PASS / FAIL  ← hybrid search canary
  1.5  show (metadata)           PASS / FAIL
  1.6  show (page text)          PASS / FAIL
  1.7  list documents            PASS / FAIL
  1.8  list collections          PASS / FAIL
  1.9  list registrations        PASS / FAIL
  1.10 list databases            PASS / FAIL  <time if >3s>
  1.11 ingest URL                PASS / FAIL
  1.12 delete document           PASS / FAIL
  1.13 delete collection         PASS / FAIL
  1.14 use database              PASS / FAIL / SKIP
  1.15 learn                     PASS / FAIL
  1.16 missions_sync (dry run)   PASS / FAIL

Phase 2: CLI (1:1 mirror of Phase 1 + CLI-only checks)
  2.1  doctor                    PASS / FAIL  <failed checks>
  2.2  status                    PASS / FAIL
  2.3  remember                  PASS / FAIL
  2.4  find (semantic)           PASS / FAIL
  2.5  find (BM25 keyword)       PASS / FAIL  ← hybrid search canary
  2.6  show (metadata)           PASS / FAIL
  2.7  show (page text)          PASS / FAIL
  2.8  list documents            PASS / FAIL
  2.9  list collections          PASS / FAIL
  2.10 list registrations        PASS / FAIL
  2.11 list databases            PASS / FAIL  <time>
  2.12 ingest URL                PASS / FAIL
  2.13 delete document           PASS / FAIL
  2.14 delete collection         PASS / FAIL
  2.15 use database              PASS / FAIL / SKIP
  2.16 version                   PASS / FAIL  <version string>
  2.17 remote list --ping        PASS / FAIL / SKIP  <status>
  2.18 capture PII redaction     PASS / FAIL
  2.19 learn                     PASS / FAIL
  2.20 missions sync --dry-run   PASS / FAIL
  2.21 unknown memory type → 400 PASS / FAIL

Phase 3: Enable/Disable
  3.1  enable                    PASS / FAIL
  3.2  enable --collection       PASS / FAIL
  3.3  enable idempotent         PASS / FAIL
  3.4  doctor enable status      PASS / FAIL
  3.5  disable                   PASS / FAIL
  3.6  disable --keep-data       PASS / FAIL
  3.7  cleanup                   PASS / FAIL

Phase 4: Install
  4.1  service unit              PASS / FAIL  <ExecStart path>
  4.2  GPU provider              PASS / FAIL / SKIP  <provider>
  4.3  port binding              PASS / FAIL  <bind address>
  4.4  TLS health                PASS / FAIL

Phase 5: Agent-Memory Write Loop (DES-055)
  5.1  SubagentStop distillation PASS / FAIL / SKIP  <handle, id8>

Result: PASS / FAIL
  Passed: N/49
  Failed: N/49
  Skipped: N/49
  Notes: <any observations, warnings, or follow-up beads created>
```

SKIP is valid for: 1.14 (single database), 2.15 (single database),
2.17 (no remote configured), 4.2 (no GPU), 5.1 (no Claude Code session
in a repo with a vendored ethos identity). Everything else must be
PASS or FAIL with explanation.
