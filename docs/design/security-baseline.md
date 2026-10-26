# quarry security and operational baseline (as of c01f195)

**Mission:** m-2026-10-25-003, phase 1 · **Worker:** rmh · **Evaluator:** djb
**Purpose:** list what quarryd does today, with DES numbers and file:line citations, so
the answer-synthesis design matches the existing product rather than inventing rules
for one channel. Each row ends with what `/v1/answer` must do to match it. Section 3
lists gaps in the baseline. Each gap is written as a fix for every quarryd channel,
not a rule only for the model endpoint.

Sources: every entry in `DESIGN.md` (DES-001 to DES-058; DES-033 and DES-035 are
reserved and empty) and the code they cite. Where the code says nothing, this document
says nothing about it, and the gaps section says "not in code".

## 1. Baseline by area

### 1.1 Transport, TLS, and pinning

| Fact | Source | What `/v1/answer` must do to equal it |
|---|---|---|
| The installed daemon always serves TLS, loopback included; `--tls` is added to the service command when `server.crt` and `server.key` exist. | DES-020; `service.py` `_quarryd_exec_args` (TLS append after `QUARRY_SERVE_HOST`) | Be served by the same listener. Add no new TLS setting. |
| `quarry install` generates an EC P-256 CA and a server cert under `~/.punt-labs/quarry/tls/`. `ca.crt` is 0644; `ca.key`, `server.crt`, and `server.key` are 0600. Each file is created with its mode at `os.open`, then renamed into place. Generation is skipped when all four files exist. | DES-020; `tls.py:288-310`, `tls.py:340-349`, `tls.py:360-365` | Nothing new on the listener. For outbound TLS, use the platform's normal verification (below). |
| Clients pin the daemon's CA (TOFU: `quarry login` shows the SHA256 fingerprint and pins it). Pinned contexts exclude system roots. The first `/ca.crt` fetch is unverified by design (TOFU, `CERT_NONE`). The TLS 1.3 minimum is enforced in mcp-proxy, not in quarry code (no `TLSv1_3` under `src/quarry`). | DES-020; `remote.py:194` (pinned), `remote.py:273` (TOFU fetch) | The CLI and menu bar reach `/v1/answer` through the existing pinned clients. |
| Outbound HTTPS from quarryd today (URL and sitemap ingest) verifies the server against the **system** trust store (`ssl.create_default_context()`) through `PinnedHTTPSConnection`, with `wrap_socket(server_hostname=...)`. httpx with `verify=True` would use the certifi bundle instead (`httpx/_config.py:40`, httpx 0.28.1 per `uv.lock:779-780`). | DES-044; `ingestion/pinned_connection.py:18-19` | Pass the same system-store context to httpx (`verify=ssl.create_default_context()`, a shared helper). Never `verify=False`, never the daemon's own CA. |

### 1.2 Authentication and token handling

| Fact | Source | What `/v1/answer` must do to equal it |
|---|---|---|
| Every `/v1` route requires `Authorization: Bearer`, compared with `hmac.compare_digest` and a case-insensitive scheme. A mismatch returns 401 `{"error": "Unauthorized"}`. `/health`, `/ca.crt`, and OPTIONS are exempt. | DES-005; `daemon/routes/base.py:47-64` | Call `reject_unauthorized` first. Use the same 401 body. |
| Every bind carries a key. The launcher mints a 256-bit loopback token when none is given. The server strips the key and refuses to serve without one. | DES-031 v2.2 R4; `daemon/launcher.py` (`_TOKEN_BYTES = 32`); `daemon/server.py` `DaemonServer._authenticated` | Nothing new. |
| A non-loopback bind needs both an operator key and TLS. A name that does not resolve counts as remote. | DES-022, DES-031; `net.py:117-145` (`enforce_bind_key`, `enforce_bind_tls`); `launcher.py:52-54` | Nothing new. |
| `serve.token` is written 0600 with `O_CREAT`, `O_EXCL`, `O_CLOEXEC`, and only after a successful bind. The run-dir `flock` makes one daemon the only writer. Clients present the live token only to a literal loopback IP. | DES-031 v2.2; `run_dir.py:98`, `run_dir.py:156-161`; `server.py` `_acquire_run_dir_lock`; `client/config.py` (module docstring) | Nothing new. |
| CORS reflects only allow-listed origins (default `http://localhost`). POST must be `application/json`. | DES-010; `daemon/app.py` `JsonContentTypeGuard`, `_add_middleware` | `/v1/answer` is a GET with a bearer token, so it gets the same CORS handling. |

### 1.3 Remote access

| Fact | Source | What `/v1/answer` must do to equal it |
|---|---|---|
| After `quarry login`, every data command routes to the remote daemon; there is no split horizon. Only `login`, `logout`, `remote list`, `install`, `uninstall`, `serve`, `mcp`, and `version` stay local. | DES-021 | `quarry answer` routes remotely like `find`. |
| A remote daemon is reached over TLS with a pinned CA and a bearer token. The daemon refuses a plaintext non-loopback bind. | DES-020, DES-022; `net.py:130-145` | No extra rule. The model endpoint gets the same posture as the daemon (section 2). |

### 1.4 Outbound HTTP (egress)

| Channel | Today | Source | What `/v1/answer` must do to equal it |
|---|---|---|---|
| URL ingest, WebFetch re-fetch, sitemap crawl (the URL comes from the caller) | Every hop is SSRF-gated against the resolved address: loopback, link-local, RFC 1918, CGNAT, and unspecified addresses are rejected, along with metadata names. Redirects are re-gated. The connect address is pinned (no DNS rebinding). `ProxyHandler({})`: `HTTP(S)_PROXY` is ignored. Each call passes a per-request `timeout`. The body read is capped at `_MAX_RESPONSE_BYTES` = 4 MiB and fails past it. | DES-043, DES-044; `url_safety.py:34-129`; `ingestion/ssrf_redirect.py:43-68`, `:134`; `ingestion/opened_response.py:21`, `:53`, `:106-107`; `sitemap_web_client.py:149` | The model URL comes from the operator, not the caller, so no SSRF gate applies; the gate is for caller-supplied URLs. Copy the rest: ignore env proxies, re-check redirects (or don't follow them), set an explicit timeout, and cap the body at the same `_MAX_RESPONSE_BYTES`. |
| mcp-proxy binary download (install time) | `urllib.request.urlopen` with the default opener: env proxies are honored, redirects are followed, system TLS. Timeouts are 15 s, 15 s, and 60 s. The SHA256 is checked against the release's `checksums.txt`. | `proxy.py:19-61`, `proxy.py:85`, `proxy.py:140` | Not comparable: this is install time and the content is fixed. |
| Embedding model download | `huggingface_hub.hf_hub_download`, pinned to `ONNX_MODEL_REVISION`. Proxy and redirect behavior belongs to `huggingface_hub` and is not set in quarry code. | DES-004; `embeddings.py:44-60`; `config.py:14-15` | Not comparable (install time, pinned revision). |
| Client to daemon | `httpx` with `trust_env=False` and a 15 s default timeout (per-call override). One retry on a refused connect. | `client/transport.py:26`, `:86-91`, `:95-122` | The model client uses `httpx` with `trust_env=False` and an explicit timeout. That is the closest existing precedent. |

### 1.5 Log safety and scrubbing

| Fact | Source | What `/v1/answer` must do to equal it |
|---|---|---|
| The uvicorn access log is off. The search route logs only a result count, at DEBUG. | DES-008; `server.py` (`access_log=False`); `daemon/routes/search.py:111` | Log no query, passage, answer, or key text. |
| quarryd writes `quarryd.log` (rotating; `QUARRY_LOG_DIR`, falling back to `~/.punt-labs/quarry/logs`). Volume rule: INFO is at most one line per user-visible operation; finer detail goes to DEBUG. The `httpx` logger is at WARNING. | DES-048; `logging_config.py:61-87`, `:149` | One INFO line per answer (host, model, status, latency, counts). Everything else at DEBUG. |
| An unhandled exception is logged with its traceback, and the client gets a generic `{"error": "Internal server error"}`. | `daemon/app.py:158-160` | Let unexpected errors fall through to this handler. Map expected errors to explicit bodies. |
| Content refusals return `{"error": str(exc)}` with 400: the exception text goes to the client. | `daemon/routes/content_group.py:~68`, `:~89` | Error bodies carry quarry's own messages. Never forward raw upstream text without scrubbing (gap G2). |
| Captures, `remember`, and `learn` are scrubbed before storage: secrets, home paths, emails (replaced with a placeholder), the local hostname, and profanity, in one choke point. Deliberately ingested documents are not scrubbed. | DES-036, DES-041; `scrub.py`, `capture.py` | Passages sent to the model are the stored text. Captures are already scrubbed; user-ingested documents are not (stated in PR/FAQ `faq:privacy`). |
| The scrubber's secret patterns cover `sk-ant-…`, `sk-…` (32 or more characters), and `Bearer …` (20 or more characters). | `scrub_rules.py:98-109` | Same scrubber, same coverage. Widening it is gap G2. |

### 1.6 Usage logging (telemetry)

| Fact | Source | What `/v1/answer` must do to equal it |
|---|---|---|
| Each search writes one `query_events` row (time, surface, agent handle, collection, scrubbed filters, limit, latency, result count, scrubbed query, query length) and one `query_hits` row per hit to `telemetry.db`. The write runs as a post-response `BackgroundTask`; failures are logged and swallowed. | DES-056; `daemon/routes/search.py:66-140`; `query_log.py:94-130` | Record each answer the same way, using the same scrubbing and the same post-response background write. Answers add `kind`, `covered`, and model latency (schema change, Phase 2). |
| `telemetry_enabled` defaults to on. Retention is 90 days with periodic pruning. The telemetry directory is 0700 and the database 0600 (`O_EXCL`, `O_NOFOLLOW`; a looser existing file is tightened). Reads go through `/v1/insights`. | DES-056; `config.py:45-53`; `telemetry_perms.py:30-68`; `daemon/routes/meta.py` `insights` | Same switch, same retention, same file. |

### 1.7 Install, configuration, and secrets

| Fact | Source | What `/v1/answer` must do to equal it |
|---|---|---|
| `quarry install` runs 8 steps with no prompts. Optional steps (headless OpenCV, mcp-proxy) are best-effort; GPU and model failures fail the install. | `doctor.py:541-650` | Add one step, before daemon registration, with no prompts. A failure in the optional step never fails the install. |
| Re-runs are idempotent for existing artifacts: TLS files are kept, the CLAUDE.md block is guarded by a sentinel, ethos extension files get a raw append only when missing, and per-project permissions land only through `quarry enable`. | DES-013, DES-019, DES-058; `tls.py:288-310` | Same: re-running keeps an existing synthesis config and only fills in what is missing. |
| The operator key reaches the daemon through the service definition. Linux: `~/.punt-labs/quarry/quarry.env` (0600 at creation, temp file plus replace), loaded by systemd `EnvironmentFile`. macOS: embedded in the launchd plist (0600 in a 0700 `LaunchAgents`). The key is never passed as argv. | DES-022; `service.py:60-93`, `service.py:166-200`, `service.py` `_systemd_unit_content` | Use the same path for the model key (or a file in the same directory with the same mode). |
| The key comes from `QUARRY_API_KEY` in the installer's environment, every run. A re-run without it rewrites `quarry.env` and the plist without the key. | `service.py:424-450`, `service.py:60-93`, `service.py:166-191` | See gap G1. |
| The systemd unit is written with `Path.write_text` (umask mode). It holds no secret. | `service.py` `_systemd_install` | Nothing new. |
| Per-user data lives under `~/.punt-labs/quarry/` (`data/`, `logs/`, `tls/`, `quarry.env`, `config.toml`); per-repo config lives in `.punt-labs/quarry/config.md`. | DES-015; `db_pointer.py:58` | Put the config under `~/.punt-labs/quarry/`. |
| No config file is permission-checked when it is read. Every check is at write time. | not in code | See gap G3. |

### 1.8 Retrieval and limits

| Fact | Source | What `/v1/answer` must do to equal it |
|---|---|---|
| Search is hybrid: vector (cosine, normalized embeddings) plus BM25 (Tantivy), fused by RRF (`k = 60`, each channel over-fetches 3x). If FTS fails, search falls back to vector-only with a WARNING. | DES-017, DES-037, DES-038; `retrieval/config.py` (`rrf_k`, `fetch_multiplier`, `metric`); `retrieval/service.py:86-95`; FTS fallback `retrieval/hybrid.py:112` | Call the same `SearchService` with the same `RetrievalConfig`. |
| Each hit carries `similarity`, a bounded cosine (FTS-only rows report their true cosine), and results come back in RRF order. There is no relevance threshold anywhere in retrieval or routes. | DES-038; grep of `retrieval/` and `routes/search.py` (no `threshold` or `min_similarity`) | Choosing what goes to the model needs either a new relevance rule or RRF order alone. No existing threshold can be reused. |
| `/v1/search` clamps `limit` to `[1, 50]`, default 10. | `routes/search.py:197-202` | Phase 2: the answer route asks for as many hits as fit the model's context budget plus 10-15%, within the same 50 ceiling or with a documented one for this route. |
| Chunks are at most 1,800 characters with 200 characters of overlap; `chunk_max_chars` is capped at 16,384. | DES-057; `config.py:55-56` | Budget arithmetic uses these numbers. |
| Every filter (`collection`, `document`, `page_type`, `source_format`, `agent_handle`, `memory_type`) is applied the same way on every surface. | DES-037; `routes/search.py` `_filter`; CLAUDE.md bug class 3 | `/v1/answer` accepts the full filter set (operator answer 7). |

### 1.9 Concurrency and overload

| Fact | Source | What `/v1/answer` must do to equal it |
|---|---|---|
| Ingest goes through one FIFO per collection with a global embed semaphore and a bounded depth (`ingest_queue_depth = 32`). A full queue returns 503 and is never dropped silently. | DES-042; `routes/base.py` `submit`; `config.py:69-73` | None. G4 is an observation only. |
| sync, optimize, and backfill are singletons and return 409 with the running `task_id`. | DES-026; `routes/base.py` `reject_if_running` | Not applicable. |
| Searches, status, and other reads have no per-client or per-daemon rate limit. | not in code | See gap G4. |
| The daemon never reports storage size, because walking the tree was an outage any client could trigger. | DES-049 | Status stays cheap: no outbound probe from `/v1/status`. |

## 2. One posture for both remote ends

The operator's answer 8 asks for one security posture for two things that can each run
anywhere: quarryd itself and the model endpoint. Today's rules for a remote quarryd,
and the matching rule for the model endpoint:

| Rule for remote quarryd today | Source | The same rule for the model endpoint |
|---|---|---|
| Non-loopback means TLS; loopback may be plaintext only for development (`quarryd` without `--tls`). The installed daemon always runs TLS. | DES-020, DES-022, `net.py:130-145` | Non-loopback endpoint means `https`. Plain `http` only to loopback (Ollama's default). No opt-out flag, matching DES-020's rejected `--insecure`. |
| Non-loopback means an operator key. | `net.py:117-128` | A key is needed whenever the endpoint requires one. quarry doesn't decide that; the endpoint returns 401. |
| Certificates are verified, and nothing ever skips verification. | DES-020 | Certificates are verified, with no `verify=False`. |
| The key is stored 0600 and never in argv. | `service.py:60-93` | Same. |

## 3. Baseline gaps: a separate hardening project

Operator decision (m-003): hardening is a separate project, and the operator decides
whether it comes before or after answer synthesis. Network and concurrency changes are
not part of the answer-synthesis work. G1, G2, G3, G5, and G6 are filed as independent
issues. G4 is an observation only. `/v1/answer` matches the baseline as it stands and
does not wait on any of them.

G1. **A re-run of install drops the operator key (issue).** `quarry install` rebuilds
`quarry.env` and the launchd plist from the current environment, so a re-run without
`QUARRY_API_KEY` exported removes the stored key (`service.py:60-93`, `:166-191`,
`:424-450`). **Fix for all keys:** install keeps an existing secret unless a new value
is supplied, the keep-what-exists rule TLS files already follow.

G2. **Secret scrubbing is narrow, and some error bodies echo exception text (issue).**
`scrub_rules.py:98-109` matches `sk-ant-`, long `sk-` keys, and `Bearer` tokens only.
`content_group.py:~68,~89` returns `str(exc)`. **Fix for all channels:** widen the
scrubber with common provider prefixes, and make error bodies quarry-written messages,
with exception detail logged at DEBUG.

G3. **Nothing is permission-checked when it is read (issue).** Files are created with
the right mode, but the mode is never checked when a file is read. **Fix for all
channels:** one secure-read helper (owner is the user, not group- or world-writable,
not group- or world-readable when it holds a secret), used for `quarry.env`,
`serve.token`, the mcp-proxy config, and `synthesis.toml`, and reported by `quarry
doctor`. In-tree precedent: `telemetry_perms.py:47-68` already tightens a looser
telemetry dir and database when it opens them.

G5. **Outbound HTTP follows two different policies (its own issue).** In plain terms:
when quarry fetches a URL a caller gave it (ingest, sitemap), it ignores
`HTTP(S)_PROXY`, re-checks every redirect, and pins the connect address
(DES-043/044). When quarry downloads something whose URL is fixed in its own code
(the mcp-proxy binary from GitHub, the embedding model from Hugging Face), it uses
the standard library defaults: it honors `HTTP(S)_PROXY` and follows redirects
(`proxy.py:60-140`; `huggingface_hub` for the model). Both are integrity-checked
(SHA256, pinned revision), so this is a consistency question, not an exposure. The
issue is to choose, once and in DESIGN.md, whether quarry honors the user's proxy
settings for URLs it did not get from a caller. The model endpoint (an operator-fixed
URL) follows whatever is chosen. Until then it uses `trust_env=False`, like the daemon
client.

G4. **Read routes have no overload limit (observation only).** Search, status,
insights, and answer have no concurrency or rate bound, unlike ingest (DES-042, 503)
and singleton jobs (DES-026, 409). DES-049 shows one read route was once an outage.
No fix is proposed: quarry runs on hardware from a laptop to a GPU server, and one
fixed limit would be wrong on most of it. If an overload is observed, it gets its own
issue then.

G6. **Graceful shutdown has no bound (issue).** No `timeout_graceful_shutdown` is set
anywhere under `daemon/`, so uvicorn waits without limit for in-flight requests. The
lifespan shutdown runs `ctx.aclose_ingest_queue()` (the DES-042 drain) only after that
wait (`server.py:278-280`). launchd sends SIGKILL 20 s after SIGTERM (the plist sets no
`ExitTimeOut`), and systemd after 90 s (the unit sets no `TimeoutStopSec`,
`service.py:334-335`). Every route finishes in milliseconds today, so this has not shown
up. A request held open across a restart would lose admitted captures. **Fix for all
routes:** set `timeout_graceful_shutdown` below the smallest service-manager kill
timeout (for example 10 s), so uvicorn cancels the remaining request tasks and the
drain still runs. Test: send SIGTERM while a request hangs (fake upstream in `hang`
mode) with one queued capture. The capture must be written and the process must exit
within the window. The operator decides when this lands.

Not gaps (checked):

- Bearer comparison is constant-time, and every bind is authenticated.
- Telemetry and token files are created with the right mode.
- The SSRF gate covers every hop of every caller-supplied fetch.

## Revision history

- **v1 (m-003 phase 1):** baseline by area, gaps G1-G5.
- **v2 (m-003 phase 2):** section 3 rewritten. Hardening is a separate project, G1, G2,
  G3, and G5 are independent issues, G4 is an observation only, and G5 is explained in
  plain terms.
- **v3 (m-003 round 2, djb review):** row 1.4 (fetch) lists the 4 MiB
  `_MAX_RESPONSE_BYTES` read cap (F1); G6 added, bounded graceful shutdown for all
  routes (F2); row 1.1 states that fetch uses the system trust store and httpx defaults
  to certifi (F3); citations corrected: `remote.py:273` is the TOFU fetch,
  `search.py:111`, `retrieval/hybrid.py:112`, the 1.9 ingest row, and the
  `telemetry_perms.py:47-68` precedent in G3 (F10).
