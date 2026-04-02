# Build Plan: quarry-g0ed — Remote CLI Parity

Epic: **quarry-g0ed** — all data commands route to remote server when configured.
No silent local fallback. Only `login`/`logout`/`remote` stay local.

## Key Decisions

1. **Generalized HTTP helper**: refactor `_remote_https_get` into
   `_remote_https_request(method, path, config, body=None)` with thin
   method-specific wrappers. Single place for TLS/auth logic.
2. **REST conventions throughout**: query params for GET/DELETE, JSON body
   for POST. No mixing.
3. **CORS**: add POST and DELETE to `allow_methods` in PR 1.
4. **Delete semantics**: 404 if resource doesn't exist (REST convention).
   CLI exits 1 for not-found on remote.
5. **Multiple small PRs**: 4 PRs to avoid review bottlenecks.

## PR 1 — Infrastructure + Read-Only Wiring

**Beads**: quarry-stcd, quarry-77uh, quarry-y1rm

| Change | File |
|--------|------|
| Refactor `_remote_https_get` → `_remote_https_request` | `__main__.py` |
| Add POST/DELETE to CORS `allow_methods` | `http_server.py` |
| Wire `list documents` → `GET /documents` | `__main__.py` |
| Wire `list collections` → `GET /collections` | `__main__.py` |
| Tests: remote routing for list commands | `test_cli.py` |

## PR 2 — Show + Delete Endpoints

**Beads**: quarry-rd10, quarry-osu5

| Change | File |
|--------|------|
| Add `GET /show` endpoint | `http_server.py` |
| Add `DELETE /documents` endpoint (404 on miss) | `http_server.py` |
| Add `DELETE /collections` endpoint (404 on miss) | `http_server.py` |
| Wire `show_cmd` to remote | `__main__.py` |
| Wire `delete_cmd` to remote | `__main__.py` |
| Tests: endpoints + CLI routing + equivalence | `test_http_server.py`, `test_cli.py` |

## PR 3 — Remember + Ingest

**Beads**: quarry-oev6, quarry-2bxm

| Change | File |
|--------|------|
| Add `POST /remember` endpoint | `http_server.py` |
| Add `POST /ingest` endpoint (URL-only first) | `http_server.py` |
| Wire `remember` to remote | `__main__.py` |
| Wire `ingest_cmd` to remote (URL path) | `__main__.py` |
| Tests: endpoints + CLI routing | `test_http_server.py`, `test_cli.py` |

## PR 4 — Remaining Write Ops

**Beads**: quarry-mclj, quarry-mn83, quarry-to48

| Change | File |
|--------|------|
| Add `POST /sync` endpoint | `http_server.py` |
| Add `GET /databases` endpoint | `http_server.py` |
| Add `POST /use` endpoint | `http_server.py` |
| Add `GET/POST/DELETE /registrations` endpoints | `http_server.py` |
| Wire `sync_cmd`, `use_cmd`, register/deregister/list-registrations | `__main__.py` |
| Tests: endpoints + CLI routing + equivalence | `test_http_server.py`, `test_cli.py` |

## Dependency Graph

```
PR 1 (infra + read wiring)
  └─► PR 2 (show + delete)
  └─► PR 3 (remember + ingest)
       └─► PR 4 (sync, databases, registrations)
```

PR 2 and PR 3 can run in parallel after PR 1 merges.
