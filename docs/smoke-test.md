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

**Verify:** Reports chunks deleted.

### 2.14 Delete collection (cleanup)

```bash
quarry delete smoke-test --type collection
```

**Verify:** Reports chunks deleted. Confirm with
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
- ExecStart points to `~/.local/share/uv/tools/punt-quarry/bin/quarry`
  (NOT `.venv/bin/python3`)
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

## Quick Pass Criteria

- Phase 1: all 14 MCP tool calls succeed, BM25 keyword match works
  (1.4), cleanup leaves no smoke-test data
- Phase 2: all 17 CLI checks succeed, BM25 keyword match works (2.5),
  cleanup leaves no smoke-test data, `list databases` completes in <3s
- Phase 3: all 7 enable/disable checks succeed, registrations created
  and removed correctly, config.md managed, doctor reports enable status
- Phase 4: service unit points at tool venv, correct bind address,
  CUDA on GPU hosts

## Quick Fail Indicators

- `find` returns results but BM25 keyword test (1.4/2.5) fails — FTS
  index stale, `optimize_table` not rebuilding index
- `doctor` shows "claude CLI timed out" — still using subprocess probe
  instead of file-based check
- `doctor` shows "FTS index: stale" — need `quarry sync` to trigger
  rebuild
- `list databases` takes >5 seconds — still using rglob instead of du
- Service ExecStart contains `.venv/bin/python3` — dev venv baked into
  unit, will crash-loop on next restart
- ONNX provider shows CPU on a GPU host — onnxruntime-gpu not installed,
  check install.sh GPU swap
- `quarry enable` crashes on child of registered parent — walk-up
  matching or descendant guard broken

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

Result: PASS / FAIL
  Passed: N/42
  Failed: N/42
  Skipped: N/42
  Notes: <any observations, warnings, or follow-up beads created>
```

SKIP is valid for: 1.14 (single database), 2.15 (single database),
2.17 (no remote configured), 4.2 (no GPU). Everything else must be
PASS or FAIL with explanation.
