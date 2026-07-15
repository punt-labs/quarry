# Client-Library Refactor — DES-031 v2.2 (MCP-as-client + `quarryd`)

**Status:** ACCEPTED — operator-ratified 2026-07-14; backing design for the
DES-031 v2.2 amendment. Amends the ACCEPTED DES-031 v2.1 (`DESIGN.md:897`,
`docs/des-031v2-daemon-first.md`). Folds in the operator's session rulings, which
**reverse** v2.1's MCP decision and add a dedicated daemon binary.
**Date:** 2026-07-14
**Author:** rmh
**Model:** vox (`voxd` daemon + `vox mcp` FastMCP-as-client subcommand).

> **What changed from v2.1.** v2.1 kept MCP *inside* the daemon (served over
> `/mcp`, bridged by mcp-proxy) and rejected a Python MCP-server-as-client
> (§4.2). The operator has ruled the opposite: **MCP is a client concern.** The
> daemon stops serving MCP; `quarry mcp` becomes a FastMCP subcommand that
> reaches the daemon through `QuarryClient` — exactly vox's `vox mcp` →
> `server.py` → `VoxClientSync` shape. Quarry's plugin stops routing its MCP path
> through mcp-proxy and runs `quarry mcp` directly (mcp-proxy itself is a live,
> supported tool used by other consumers — nothing about it changes). A dedicated
> `quarryd` binary becomes the engine process, making I1 a hard **process**
> boundary. All seven rulings below are **DECIDED**, not open.

---

## 1. The seven rulings (DECIDED)

| # | Ruling | Reverses / adds |
|---|--------|-----------------|
| R1 | **MCP is a client concern.** Daemon does NOT serve MCP. Delete the daemon `/mcp` route + all daemon-side MCP machinery. `quarry mcp` = a FastMCP stdio subcommand reaching the daemon via `QuarryClient`. Plugin runs `quarry mcp` directly. | **Reverses** v2.1 §3.5/§4.2 |
| R2 | **Quarry's plugin stops routing MCP through mcp-proxy.** `plugin.json` drops the `mcp-proxy … else quarry mcp` shim → just `quarry mcp`. The auth/reconnect the proxy handled for quarry's path move into `QuarryClient`. **mcp-proxy remains a supported, in-use tool for other consumers — nothing about mcp-proxy changes.** | Drops quarry's use of the DES-021 transport |
| R3 | **`quarryd` binary.** New `[project.scripts]` entry point (like `voxd`), the engine process, replacing the in-process `quarry serve` subcommand. Only `quarryd` imports the engine. | **Adds**; hardens v2.1 §3.1 |
| R4 | **`serve.token` (mode-0600).** Daemon writes it; a new `ClientConfig` resolves port+token; loopback requests are authenticated. | v2.1 §7 amendment |
| R5 | **Seam = `QuarryClient` + injectable REST transport.** No vox-Gateway-Protocol, no biff-commands layer. Keep the in-process `httpx.ASGITransport` fixture over the real handlers. | **Confirms** v2.1 §3.4/§5 |
| R6 | **`quarry-hook` through `QuarryClient`**, not `Database.connect` — I1-correct and faster. | **Confirms** v2.1 §3.1 |
| R7 | **Staged streamable-HTTP-on-the-daemon work is DISCARDED.** It kept MCP in the daemon (wrong direction). The MCP deprecation resolves by DELETING daemon MCP, not migrating it. | **Discards** in-flight task #3 |

---

## 2. Target topology — a hard process boundary (R3)

Vox is the exact template: `voxd = "punt_vox.voxd:entrypoint"` (daemon) and
`vox = "punt_vox.__main__:app"` (CLI) are **separate binaries**
(`vox/pyproject.toml:45-46`); `vox mcp` (`vox/__main__.py:958`) runs
`server.py`'s FastMCP, whose tools hold
`ClientProgramGateway(VoxClientSync())` (`server.py:294`) — the MCP server is a
**client** of voxd. Claude Desktop registers `["--from","punt-vox","vox","mcp"]`
(`vox/__main__.py:877`) — the subcommand directly, **no proxy**.

Quarry adopts the same shape:

```text
  quarry/api/        ← LAYER 1: wire contract. Pydantic models + errors.
  (schemas, errors)    Zero engine deps. Importable alone.
        ▲     ▲
        │     │
  quarry/client/     ← LAYER 2: QuarryClient. REST(S) transport + typed errors
  (QuarryClient,       + ClientConfig (port/token resolution) + auth/reconnect
   ClientConfig)       (absorbed from mcp-proxy, R2). Imports quarry.api only.
        ▲   ▲   ▲
        │   │   └───────────────┐
  quarry/__main__   quarry/mcp_server   quarry/hooks   ← LAYER 3: CLIENT PROCESSES
  (quarry CLI)      (quarry mcp:         (quarry-hook)     each a pure client;
                     FastMCP→QuarryClient)                 NONE import the engine.
        …
  ─────────────────────────  PROCESS BOUNDARY  ─────────────────────────
  quarryd (entry point)   ← THE ENGINE PROCESS (server side ONLY).
  quarry/daemon/            Owns Database, embeddings, ingestion, retrieval
  quarry/db, /retrieval,    (SearchService), SyncRegistry. Imports quarry.api
  /ingestion, /embeddings   to validate/serialize. Serves REST only — NO MCP.
```

**Entry points** (`pyproject.toml:78-80`, today `quarry` + `quarry-hook`):

| Binary | Today | v2.2 |
|--------|-------|------|
| `quarry` | CLI + `serve` + `mcp` subcommands, in-process engine | CLI, pure client (`quarry serve` retires) |
| `quarry-hook` | `_hook_entry:main`, opens `Database` | pure client via `QuarryClient` (R6) |
| **`quarryd`** | — | **new**: `quarry.daemon.__main__:entrypoint`, the engine; supervised unit execs it |
| `quarry mcp` | subcommand → in-process FastMCP over `Database` | subcommand → FastMCP over `QuarryClient` (R1) |

**Why `quarryd` matters for I1.** v2.1 enforced the boundary with import-linter +
a lazy in-body engine import for `serve`/`mcp` (the two commands that legitimately
needed the engine) — a *within-process* exception guarded by a runtime-sabotage
test (v2.1 §3.1). With R1+R3 that exception **disappears**: `mcp` no longer needs
the engine (it is a client), and `serve` becomes the separate `quarryd` binary.
So **every** `quarry`/`quarry mcp`/`quarry-hook` process is now engine-free with
**no** lazy-import carve-out. I1 becomes a clean process split enforced by package
membership: only `quarry/daemon/` (reachable solely from `quarryd`) imports
`quarry.db`/`embeddings`/`ingestion`/`retrieval`/`sync`. import-linter still
guards it statically; the sabotage test simplifies (no command is exempt).

---

## 3. `quarry mcp` — FastMCP over `QuarryClient` (R1, R2)

**Today** `mcp_server.py` (526 lines) is an in-process engine: every tool calls
`_database()` → `Database.connect(...)` (`mcp_server.py:75-77`),
`get_embedding_backend` (`mcp_server.py:124`), `SearchService(database)`
(`mcp_server.py:133`). **v2.2** keeps the FastMCP tool *definitions* and their
docstrings (the MCP surface Claude Code sees is unchanged) but rewrites every
tool body to call `QuarryClient` instead of the engine — the vox `server.py`
pattern, where a module-level client value backs the tools
(`server.py:294`, `_program_tools = ClientProgramGateway(VoxClientSync())`):

```python
# mcp_server.py (v2.2) — client-tier, engine-free
_client: QuarryClient = QuarryClient.connect(ClientConfig.resolve())

@mcp.tool()
@_handle_errors
def find(query: str, limit: int = 10, ...) -> str:
    resp = _client.search(SearchRequest(query=query, limit=min(limit, 50), ...))
    return format_search_results(query, [h.model_dump() for h in resp.hits])
```

- `main()` (`mcp_server.py:520-522`) still `mcp.run(transport="stdio")` — identical
  to vox (`server.py:935`). The `_db_name` ContextVar (`mcp_server.py:54`) maps to
  a `UseRequest`/`ClientConfig` db selection carried on the client.
- The engine imports at the top of `mcp_server.py`
  (`Database`, `get_embedding_backend`, `SearchService`, `ingestion.*`,
  `SyncRegistry` — `mcp_server.py:12-37`) are **deleted**; the module imports only
  `quarry.client`, `quarry.api`, and `quarry.formatting`. This is what makes
  `quarry mcp` a pure client and lets the sabotage test cover it.
- The background-thread machinery (`_executor`, `_background`,
  `mcp_server.py:79-91`) is **removed** — "fire-and-forget" now means the daemon
  returns `202 TaskAccepted`; the tool returns immediately with the task id, the
  daemon owns the work. No client-side thread pool.

**Quarry's plugin stops using mcp-proxy (R2).** `plugin.json`
(`.claude-plugin/plugin.json:9-18`) today runs a shell shim: `if command -v
mcp-proxy … exec mcp-proxy --config quarry; else exec quarry mcp`. v2.2 replaces
the whole `mcpServers.quarry` block with the vox-style direct spawn:

```json
"mcpServers": { "quarry": { "type": "stdio", "command": "quarry", "args": ["mcp"] } }
```

Remote access no longer needs a proxy on quarry's path: `quarry mcp`'s
`QuarryClient` reads the login config (remote URL + pinned CA + bearer,
`remote.py:29-93`) and connects to the remote daemon over REST/TLS directly — the
auth + reconnect the proxy used to handle for quarry now live in `QuarryClient`
(R2). **mcp-proxy itself is unaffected** — it remains a live, supported tool used
by other consumers; only quarry's plugin stops routing its MCP path through it.

---

## 4. Daemon serves REST only — delete all daemon MCP (R1, R7)

The daemon (`quarryd`) keeps its 20 REST routes (`http_server.py:1176-1199`) and
**drops the MCP route entirely**. Deleted:

- the `/mcp` route registration (`http_server.py:1194-1205`, the
  `McpStreamableEndpoint` mount);
- the **staged** streamable endpoint `src/quarry/mcp_transport.py`
  (`build_session_manager`, `McpStreamableEndpoint`) — **DISCARDED** (R7): it
  invested in keeping MCP in the daemon, the wrong direction;
- the session-manager lifespan wiring in `serve()`
  (`http_server.py:1317-1336`: `build_session_manager()`, `session_manager.run()`);
- any remaining WS MCP server (the prior `run_mcp_session` / websocket handler —
  the staged tree already deleted `tests/test_mcp_websocket.py`, confirming this
  path is on its way out; v2.2 finishes the removal).

**This resolves the 1.19.1 MCP-transport blocker by deletion, not migration.**
The pyright/fd problems the WS→streamable migration was chasing (task #3) vanish
with the code. `serve()` shrinks to a plain REST uvicorn server; the FD-leak
telemetry (`http_server.py:1324`, `FdTelemetry`) and port-file writing
(`http_server.py:1356-1369`) stay.

---

## 5. `serve.token` — loopback authentication (R4)

**Gap today:** the daemon writes only `serve.port`
(`http_server.py:1252-1255,1312`); loopback is unauthenticated
(`_validate_host_key` requires `--api-key` only for non-loopback binds,
`http_server.py:1275-1283`). On a multi-user host any local user can hit
`127.0.0.1:8420`.

**v2.2 (vox-parity).** Vox reads **both** `serve.port` and `serve.token`
(`vox/client.py:82-97`) and resolves host/port/token from `VOXD_*` env or the run
dir (`vox/client.py:144-164`). Quarry mirrors it:

- `quarryd` generates a random token at startup and writes
  `~/.punt-labs/quarry/data/<db>/serve.token` **mode-0600, atomically** — reuse
  the existing `os.open(..., 0o600)` + tmp-rename pattern proven in
  `store_ca_cert` (`remote.py:277-303`) / `write_proxy_config`
  (`remote.py:70-93`). It is the daemon's `api_key` for loopback.
- `_validate_host_key` no longer permits unauthenticated loopback: the daemon
  always has a token (generated if `--api-key` unset).
- New `ClientConfig.resolve()` (Layer 2) is the single target resolver, replacing
  the per-command `_safe_proxy_config()` fork (`__main__.py:262-268,302-303`):
  1. explicit env (`QUARRY_URL`/`QUARRY_TOKEN`) →
  2. remote login config (`read_proxy_config()`, `remote.py:29-39` → URL + `ca_cert` + bearer) →
  3. loopback: `serve.port` + `serve.token` from the run dir.
  This is quarry's analogue of vox's `DaemonEnv` + `read_port_file`/`read_token_file`.

Fold into DES-031 §7: the "local daemon requires the bearer token even on
loopback … mode-0600" sentence becomes **implemented**, not aspirational.

---

## 6. The seam — `QuarryClient` + injectable transport (R5)

Unchanged from v2.1 §3.4 and confirmed by the operator. **No** vox-Gateway
Protocol, **no** biff-commands layer — one seam, `QuarryClient`, parameterized by
a `ClientConfig` and an injectable REST transport:

- Production: real loopback/remote transport (httpx over TLS + pinned CA,
  extracting `RemoteClient._open_connection`, `remote_client.py:216-241`).
- Tests: `httpx.ASGITransport` over the real FastAPI app built by `build_app(ctx)`
  (`http_server.py:1160`) with a temp LanceDB + stub embedder — the "fake" runs
  the **real** handlers, so it cannot drift from production (v2.1 §5). This is why
  a 20-method Gateway Protocol + hand-written `FakeGateway` was rejected: quarry
  has ~20 ops vs vox's 6, and a domain fake reintroduces the bug-class-3 drift the
  single shared-Pydantic contract exists to kill.

Public surface = one method per REST row (v2.1 §3.4,
`docs/des-031v2-daemon-first.md:530-550`): `search`, `ingest`, `remember`, `show`,
`status`, `list_documents`/`list_collections`/`list_registrations`/
`list_databases`, `use`, `delete_document`/`delete_collection`, `register`,
`deregister`, `sync`, `optimize`, `backfill_sessions`, `captures_push`,
`await_task`. Typed `QuarryError` hierarchy replaces `typer.Exit`/`SystemExit`
(v2.1 §3.4). `RemoteClient` (`remote_client.py`) is **deleted** — extracted into
`QuarryClient`.

**`quarry-hook` (R6).** Rewrite `hooks.py` to call `QuarryClient`
(`ingest_content`/`ingest_url` → client calls) instead of `Database.connect(...)`
(`hooks.py:405`). Both I1-correct (hooks are Layer-3 clients) and **faster**: the
warm daemon answers over loopback in ms vs the ~1.6 GB cold ONNX+LanceDB load the
current in-process path pays inside the ~100 ms hook budget. `_hook_entry`/
`run_hook` stay fail-open so a down daemon never blocks Claude Code.

---

## 7. Revised write-set

**New:**

- `quarryd` entry point → `quarry/daemon/__main__.py:entrypoint`
  (`pyproject.toml` `[project.scripts]`).
- `quarry/daemon/` package — the engine process home (absorbs `http_server.py`
  `serve()` + `_QuarryContext` + routes; imports `quarry.db`/`embeddings`/
  `ingestion`/`retrieval`/`sync`).
- `quarry/api/` — Pydantic request/response/error schemas (v2.1 §3.3).
- `quarry/client/` — `QuarryClient`, `ClientConfig`, typed `QuarryError`,
  injectable REST transport, absorbed proxy auth/reconnect (R2).
- `serve.token` writer in `quarryd` (R4).

**Changed → thin/client:**

- `quarry/__main__.py` — 18 `RemoteClient`/engine sites → `QuarryClient`; `serve`
  subcommand retires (→ `quarryd`); `_cli_errors` (`__main__.py:239-253`) grows a
  `QuarryError`→`typer.Exit` mapping; `mcp` subcommand stays but now launches the
  client-tier FastMCP.
- `quarry/mcp_server.py` — tool bodies → `QuarryClient` (R1); engine imports
  (`mcp_server.py:12-37`), `_database`/`_settings`/`_executor`/`_background`
  (`mcp_server.py:71-91`) deleted.
- `quarry/hooks.py` — `Database.connect` → `QuarryClient` (R6).
- `.claude-plugin/plugin.json` — shim → `quarry mcp` direct (R2).

**Deleted:**

- `quarry/remote_client.py` (`RemoteClient` → `QuarryClient`).
- Daemon MCP: `/mcp` route (`http_server.py:1194-1205`), the session-manager
  lifespan (`http_server.py:1317-1336`), and the **staged** `mcp_transport.py`
  (R7, DISCARDED).
- The `/sync/{task_id}` + `/ingest/{task_id}` alias routes
  (`http_server.py:1190-1191`, v2.1 PL-PP-1).
- mcp-proxy shim from `plugin.json`; `write_proxy_config`/`read_proxy_config`
  logic in `remote.py` is repurposed as the remote arm of `ClientConfig` (the CA
  fetch/pin + login stays; the proxy-TOML shape may retire).

**Kept internal (engine, `quarry/daemon/`-only):** `db/`, `retrieval/`
(`SearchService` reused as `/v1/search` impl, v2.1 §3.6), `ingestion/`,
`embeddings.py`, `sync.py`/`SyncRegistry`.

---

## 8. Revised v2-x sequence

| PR | Scope (rollback-coherent) | Rulings |
|----|---------------------------|---------|
| **v2-2 (contract + FastAPI + REST-only daemon)** | `quarry/api/` schemas+errors; FastAPI; `/v1` prefix; `+optimize`/`+backfill` endpoints; `state`/version in `/health`; **delete daemon `/mcp` + staged `mcp_transport.py` + session-manager lifespan**; remove task-status alias routes; `make openapi`. | R1, R7 |
| **v2-3a (`quarryd` + supervision + ClientConfig + serve.token)** | New `quarryd` entry point + `quarry/daemon/` package (move `serve()` in; `quarry serve` retires); supervised units exec `quarryd` (launchd `KeepAlive` / systemd `Restart=always`); autostart-nudge helper; `ClientConfig.resolve()`; **`serve.token` mode-0600**. Lands before clients drop engine paths. | R3, R4 |
| **v2-3 (`QuarryClient` + CLI)** | `quarry/client/` (`QuarryClient` + typed errors + absorbed proxy auth/reconnect); **delete `RemoteClient`**; 18 `__main__.py` sites → client; `_cli_errors`→exit mapping; `disable` chunk-purge → daemon call. | R2, R5 |
| **v2-4 (`quarry mcp` as client + point quarry's plugin at `quarry mcp` directly)** | Rewrite `mcp_server.py` tools → `QuarryClient`; delete its engine imports + thread pool; `plugin.json` → `quarry mcp` direct (no mcp-proxy in quarry's path); install rewrites user `.mcp.json`. | R1, R2 |
| **v2-5 (`quarry-hook` + library API)** | `hooks.py` → `QuarryClient` (R6); `__init__.py` engine exports (`__init__.py:33-55`) → `QuarryClient` as the public library API. | R6 |
| **v2-6 (tests/install)** | Install verifies `quarryd` `/health` (`state==ready`); session-scoped in-process `ASGITransport` fixture; import-linter boundary (now no lazy-import carve-out — R3); engine-sabotage test covers `quarry mcp` too; one real-loopback-TLS smoke test. | R3, R5 |

Ordering rule unchanged: the contract (v2-2) precedes clients dropping engine
paths; `quarryd`+supervision (v2-3a) precede fallback removal. No shims (PL-PP-1).

---

## 9. 1.19.1 sequencing — recommendation

The 1.19.1 blocker was the daemon MCP transport (task #3, WS→streamable). Under
R1/R7 the fix is **deletion**. Two paths:

**(a) Minimal interim — recommend.** Discard the staged streamable work; **delete
the daemon `/mcp` route + daemon MCP machinery** (§4); point `plugin.json` at
`quarry mcp` stdio directly (R2 lands early). The interim `quarry mcp` **still
loads the engine in-process** (`mcp_server.py` unchanged) — a **temporary,
explicitly-labeled I1 violation**, local-only. pyright goes green (the WS/
streamable code causing it is gone), ship 1.19.1. v2-2…v2-5 then build the
I1-clean end state (`quarryd` + `QuarryClient` + `quarry mcp`-as-client).

- **Trade-off:** accepts (i) a one-release in-process-engine `quarry mcp` (already
  shipped behavior, fully tested), and (ii) temporary loss of **remote** MCP —
  deleting daemon `/mcp` removes the mcp-proxy remote target, and the client-side
  remote path isn't built until v2-4. Remote **CLI** still works (RemoteClient).
  Remote MCP returns in v2-4, I1-clean. Net: unblock now by deleting the wrong-
  direction code, pay the client build down incrementally.

**(b) I1-clean ship.** Build `quarryd` + enough `QuarryClient` + `quarry
mcp`-as-client to ship 1.19.1 with MCP out-of-daemon cleanly.

- **Trade-off:** 1.19.1 absorbs most of the epic (v2-2/v2-3a/v2-3/v2-4) — no
  longer a point release; weeks not days; larger rollback unit. The blocker does
  not warrant it.

**Recommendation: (a).** It resolves the deprecation the way the operator
specified ("by DELETING daemon MCP, not migrating it"), unblocks 1.19.1 in a
small diff, and the temporary I1 violation is bounded to one release with a clear
retirement path. Label the interim `quarry mcp` in-process engine as a known
transitional violation in the 1.19.1 CHANGELOG so it is not mistaken for the end
state.

---

## 10. Risks

| Risk | Mitigation |
|------|-----------|
| Deleting daemon `/mcp` drops **remote** MCP until v2-4 | Bounded, sequenced: remote CLI unaffected (RemoteClient/QuarryClient); remote MCP returns I1-clean in v2-4 via `QuarryClient`'s TLS/pinned-CA path (`remote.py:29-93`). Call it out in the 1.19.1 CHANGELOG. |
| Interim `quarry mcp` keeps the engine in-process (1.19.1) | Temporary, one release, already-tested behavior; labeled transitional; retired in v2-4. |
| Two ONNX models if `quarryd` and an in-process `quarry mcp` run at once (interim) | Only in the 1.19.1 interim; v2-4 makes `quarry mcp` a client (one resident engine in `quarryd`). Matches DES-031 §1.8-1 intent. |
| `quarry mcp` latency now crosses a socket (was in-process) | Loopback REST to a warm daemon is ms-scale; the daemon is resident (I2). Net faster than cold in-process for hooks (R6); negligible for interactive MCP tools. |
| `serve.token` file races / perms | Reuse the proven atomic `os.open(0o600)` + tmp-rename pattern (`remote.py:277-303`); CLAUDE.md class-1 file-I/O tests (fd-close-on-raise, tmp-cleanup, mode-from-creation) apply. |
| Quarry dropping mcp-proxy from its MCP path breaks users mid-upgrade | v2-4 install step rewrites `.mcp.json`/`plugin.json` to `quarry mcp`; no `old=new` shim (PL-PP-1); the shell shim already falls back to `quarry mcp` when the proxy/config is absent (`plugin.json:15`), so absence is safe. mcp-proxy stays installed and usable by other consumers. |
| `quarryd` split touches supervised units + install | v2-3a lands units + nudge before clients assume the daemon; install `/health` gate in v2-6. |
| Losing the staged streamable work feels wasteful | It was wrong-direction (kept MCP in the daemon); the correct end state has no daemon MCP at all, so there is nothing to salvage — deletion is the fix (R7). |

**Trust note (unchanged + strengthened).** Default binding loopback; remote opt-in
over the existing TLS + pinned-CA (`remote.py:216-274`, preserved in
`QuarryClient`). R4 **closes** the loopback-unauthenticated gap v2.1 only
aspired to. Dropping mcp-proxy from quarry's MCP path removes a moving part from
quarry's trust path (auth now terminates in `QuarryClient`, not a Go proxy) —
mcp-proxy itself is unchanged and stays in use elsewhere.

---

## 11. ADR delta

Amend **DES-031 to v2.2** (in-place in `DESIGN.md:897`; this doc is the backing
design, as `docs/des-031v2-daemon-first.md` backs v2.1). The v2.2 amendment:

- **Reverses §3.5 + §4.2:** MCP is a client concern; the daemon serves REST only;
  `quarry mcp` is a FastMCP-as-client subcommand (vox `vox mcp`/`server.py`
  model); quarry's plugin stops routing its MCP path through mcp-proxy; the staged
  daemon-streamable work is discarded (R1, R2, R7).
- **Adds `quarryd`** as the engine entry point, making I1 a process boundary and
  removing the v2.1 §3.1 lazy-import carve-out for `serve`/`mcp` (R3).
- **Implements §7's loopback auth** via `serve.token` + `ClientConfig` (R4).
- **Confirms** §3.4 seam (`QuarryClient` + injectable transport) and §3.1 hook
  routing (R5, R6).
- Rewrites the §6 PR table per §8 above.

DES-037 (`SearchService`) stays as `quarryd`'s `/v1/search` impl. On DES-021:
**quarry no longer uses the DES-021 mcp-proxy transport for its MCP path** (the
plugin runs `quarry mcp` directly) — but mcp-proxy itself is unaffected and
remains in use elsewhere. This is a change to quarry's own routing, not a
supersession or retirement of the tool; note it that way in the v2.2 amendment.

---

## 12. Decision-ready summary

All seven rulings are folded as DECIDED. Nothing here is open. One go/no-go item
remains for the operator:

1. **1.19.1 path:** approve **(a) minimal interim** (§9) — delete daemon MCP,
   plugin → `quarry mcp` stdio (interim in-process engine, labeled), ship; full
   client refactor after. (Recommended.)

mcp-proxy is unaffected throughout — quarry simply stops routing its own MCP path
through it. Everything else is execution of the revised v2-2…v2-6 sequence (§8).
