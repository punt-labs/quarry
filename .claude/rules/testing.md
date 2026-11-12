---
paths:
  - "src/**"
  - "tests/**"
  - "**/*_test.py"
  - "**/test_*.py"
  - "conftest.py"
  - "scripts/**"
  - "install*.sh"
---

# Testing

## Pyramid

| Layer | Make target | Runs in CI | What it covers |
|-------|-------------|------------|----------------|
| Unit | `make test` | yes | DB, embedding, search, CLI, doctor, hooks, enable/disable, service, install scripts |
| Resource-invariant | `make test` (marker `resource`) | yes | Long-lived-process leak guards: a single connection over many optimize cycles must not leak file descriptors (`tests/test_resource_invariants.py`). The daemon holds a connection for its whole lifetime; `create_fts_index(replace=True)` supersedes an index generation and LanceDB's Rust core never evicts the deleted-file readers, so a leak here is invisible to short-lived CLI tests and only surfaces as EMFILE → HTTP 500 in the daemon. |
| Integration | `make test-slow` | no (needs real ONNX model) | Real filesystem + ONNX model end-to-end. Carries both `slow` and `embedding`; the `embedding` marker is what lets the real model load, so removing it silently reroutes the tier through the fake. |
| Shell scripts | `make test` (via pytest) | yes | Install script ordering, shellcheck |
| HTTP API contract | `make test` | yes | Endpoint shape, params, response fields (growing) |
| Wheel install | `make test-wheel` | local pre-PR gate | Build wheel → isolated venv → serve on 8422 → smoke checks |
| MCP smoke test | `docs/smoke-test.md` | post-release manual | MCP + CLI + enable/disable + install verification (see `docs/smoke-test.md` for the current check count) |

`make check-full` = `make check` + `make test-wheel`. Full test suite needs `timeout=300000` on the Bash tool (5 minutes). During development, use targeted tests: `uv run pytest tests/test_specific.py -v`.

## The suite is hermetic and bounded (DES-047)

A run writes nothing under the operator's real `~/.punt-labs/quarry/` and loads no ONNX model. Four mechanisms enforce that; none of them are optional, and each exists because the obvious cheaper version was measured and does not work.

- **`HOME` is redirected** to a session temp directory by `tests/hermetic_env.py`, which the **rootdir** `conftest.py` imports. That location is load-bearing: `tests/conftest.py` imports quarry at module scope, and quarry decides its home-derived paths when its modules are imported, so `pytest_configure` is too late and a `-p` plugin is too early (the rootdir is not yet on `sys.path`). Setting the variable rather than patching `Path.home` is also required — `os.path.expanduser` reads `$HOME` and ignores the patched classmethod.
- **The embedding fake is installed at the factory**, not at import sites. An autouse fixture replaces `get_embedding_backend`, `new_embedding_backend`, *and* `quarry.embeddings.OnnxEmbeddingBackend`; the third is the one that does the work, because `streaming` and `http_resources` bind their factory with a module-scope from-import. An `onnxruntime.InferenceSession` guard fails any unmarked test that reaches a real model. **Do not add a `patch("...get_embedding_backend")` to dodge a model load** — it is already handled, and ~40 such patches were deleted. Patch locally only to assert on specific vectors.
- **Two distinct opt-outs.** `@pytest.mark.embedding` grants the real 410 MB model and is deselected by default alongside `slow`. The `real_embedding_factory` fixture keeps the real factory for tests that assert on what it *builds* (`test_backends`, `test_embeddings`, doctor's install path); those stay in the default run and still may not load a model.
- **Two session invariants**: no non-daemon thread outlives the run, and the production `config.toml` is unchanged. The thread check is session-scoped because LanceDB starts its tokio pool lazily — a per-test check fails on correct code. The file guard watches only that one quiescent **file**: the log and `registry.db` were dropped because the live daemon and MCP clients write them continuously (the guard sees files, not writers, so every firing accused an innocent test) while the `HOME`/`QUARRY_ROOT`/`QUARRY_LOG_DIR` redirects already make them unreachable from tests. A directory `mtime` does not move when a file below it is written, and a recursive walk of the operator's 15 GB tree does not finish — do not "improve" the guard into a tree fingerprint, and do not re-add the daemon-written files.

Two standing rejections, so they are not reinvented: **no `pytest-xdist`** (the suite is I/O-bound at 0.69 average cores; workers multiply the footprint for no gain) and **no workspace-wide test-concurrency lock** (it would bake the punt-labs checkout layout into a shipped product). Both are rejected, not deferred. See DES-047.

## What good testing means in this project

Quarry has four surfaces (CLI, MCP, HTTP, plugin) backed by the same core. Every feature must work on all surfaces or explicitly document which surfaces it applies to. The recurring failure mode is surfaces drifting — a parameter added to the CLI but missing from the HTTP API, or a response field present locally but omitted remotely. The testing rules below exist because these bugs appeared repeatedly and were expensive to find.

**Never retry a command that produces no output.** Diagnose first — empty output usually means a silent exception or a missing code path, not a transient failure.

## Recurring bug classes (quarry-ccji-tls, 10 review rounds)

Ten review cycles on the TLS remote-access feature revealed five classes of bugs that appeared repeatedly. Each class points to a testing gap that must be closed with any future change in that area. These are evaluator checklists — every code review must check for these.

**Class 1 — File I/O safety.** `os.write()` is not guaranteed to write all bytes. `os.fdopen()` can raise before taking ownership of the fd, leaking it. Atomic rename must be inside the try block or the temp file leaks on failure. Permissions race: creating a file then chmoding it leaves a window.

*Required tests:* Every function that uses `os.open()`/`os.fdopen()` must have tests covering (a) successful write, (b) fd explicitly closed when `os.fdopen()` raises, (c) temp file removed on any write failure, (d) file created with correct mode from the start (not chmod after). Mock `os.fdopen` to raise and assert the fd is closed and the temp file is gone.

**Class 2 — Exception boundaries.** Functions that promise `(bool, str)` or a clean fallback can silently propagate exceptions when a dependency raises before the `try` block. `ssl_ctx.load_verify_locations()` outside the try block crashes instead of returning `(False, reason)`. `read_proxy_config()` raising `ValueError` on a malformed TOML crashes CLI commands that should fall back to local mode. Install scripts that do not gate on subprocess exit codes print success after failure.

*Required tests:* Every function returning `(bool, str)` must have a test that makes the underlying call raise and verifies the function returns `(False, <non-empty string>)` rather than propagating. Every CLI command that reads optional config must have a test with malformed config that verifies fallback (exit 0, warning printed) not crash.

**Class 3 — Remote/local divergence.** The same logical operation (e.g. `quarry find`) has two code paths: local (DB) and remote (HTTP). These paths drift: the HTTP `/search` endpoint used the vector-only `search()` while the CLI used `hybrid_search()`; the `/search` route ignored `agent_handle`, `memory_type`, `document` params that the CLI sent; the remote JSON response omitted `page_number`, `page_type`, `source_format` that the local response included.

*Required tests:* For every CLI command with a remote path, write an equivalence test: call the command twice (once mocked to local, once mocked to remote HTTP), assert the JSON output contains exactly the same field names. For every query param the CLI encodes into the URL, write an HTTP server test asserting the server reads that param and passes it to the database query. A new filter on the local path must fail a test until it is also on the remote path.

**Class 4 — TLS semantics.** IP addresses require `x509.IPAddress()`, not `x509.DNSName()` — TLS clients reject the latter per RFC 5280. `not_valid_before(now)` causes "not yet valid" rejections on clients with minor clock skew; certificates should backdate by at least 5 minutes. A new CA cert context must exclude system roots entirely (`ssl.PROTOCOL_TLS_CLIENT` + `load_verify_locations` only) — using `ssl.create_default_context()` accepts any system-trusted cert, defeating pinning. CA cert and key must be verified to match before reusing them.

*Required tests:* Cert generation tests must assert: (a) IP hostnames produce `x509.IPAddress` SANs, not `x509.DNSName`; (b) `not_valid_before` is at least 1 second in the past relative to `datetime.now(UTC)`; (c) the SSL context used for pinned-CA connections has no system roots (verify by checking `ctx.verify_mode == CERT_REQUIRED` and that `ctx.get_ca_certs()` returns only the pinned cert); (d) mismatched CA cert/key raises `ValueError` before any cert is written.

**Class 5 — Install script logic.** Shell scripts have no test coverage beyond shellcheck. Logic bugs — checking API key after a slow download, service registering on loopback while the script runs on 0.0.0.0, never creating `quarry.toml` so the plugin silently falls back — are invisible to shellcheck and only caught by manual testing or Bugbot.

*Required tests:* At minimum, every install script must pass `shellcheck -x`. For logic correctness: write integration tests that invoke the scripts with a mock `quarry` binary (a shell function that records its invocations and returns success/failure). Assert: (a) QUARRY_API_KEY is checked before any slow step; (b) the service command baked into launchd/systemd includes `--host 0.0.0.0` when `QUARRY_SERVE_HOST=0.0.0.0` is set; (c) the script exits non-zero when the daemon fails to start; (d) `quarry login localhost --yes` is called after the daemon starts.

## Testing rules

1. **No new `os.open()`/`os.fdopen()` pattern without a failure-injection test** covering fd closure and temp file cleanup.
2. **No new `(bool, str)` return function without a raises-then-returns-false test.**
3. **No new CLI filter param without a matching HTTP server test** asserting the param reaches the database query.
4. **No new remote code path without an equivalence test** asserting JSON field names match the local path.
5. **No new cert generation call without asserting** SAN type (IP vs DNS), `not_valid_before` is in the past, and pinned context excludes system roots.
6. **Shell scripts must pass `shellcheck -x` in CI.** Logic tests via mock quarry binary for any script with conditional branching on quarry subcommand results.
