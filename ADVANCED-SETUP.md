# Advanced Setup

Quarry works with zero configuration out of the box — see the main
[README](README.md) for the standard install and daily usage. This document
covers configuration and deployment beyond the default: environment
variables and running the engine on a remote/GPU host.

## Environment Variables

These customize the local daemon; none are required for a default install.

| Variable | Default | Description |
|----------|---------|-------------|
| `QUARRY_PROVIDER` | *(auto)* | ONNX execution provider: `cpu`, `cuda`, or unset (auto-detect) |
| `QUARRY_API_KEY` | *(none)* | Bearer token for `quarryd` (required for a non-loopback bind) |
| `QUARRY_ROOT` | `~/.punt-labs/quarry/data` | Base directory for all databases |
| `CHUNK_MAX_CHARS` | `1800` | Max characters per chunk (~450 tokens) |
| `CHUNK_OVERLAP_CHARS` | `200` | Overlap between consecutive chunks |

The full configuration reference is in [docs/architecture.tex](docs/architecture.tex).

Quarry uses `~/.punt-labs/quarry/cache/` for its embedding model and tokenizer
(FS-4). Explicit overrides take precedence in this order: `HF_HUB_CACHE`,
`HUGGINGFACE_HUB_CACHE`, `HF_HOME/hub`, then `XDG_CACHE_HOME/huggingface/hub`.
Relative overrides are resolved from the user's home, not the daemon's or
the CLI's working directory.
Managed services keep the override selected when you run `quarry install`;
rerun it to change or clear that override. When a managed service is
installed, `quarry doctor` checks its cache even from a different shell.

## Remote Server

Run quarry on a GPU host and connect from any Mac or Linux client over TLS. On the server, set an API key and install in network mode (binds `0.0.0.0`, registers a service, prints a CA fingerprint):

```bash
export QUARRY_API_KEY=$(openssl rand -hex 32)
```

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/42baaf3/install.sh | sh -s -- --network
```

On the client, install normally, then log in — queries go to the server over HTTPS, and the server's certificate is pinned the first time you connect (trust on first use):

```bash
quarry login <server-hostname> --api-key <token>
```

## Upgrades

Re-running the Quick Start installer upgrades quarry. It:

- installs every dependency at the version tested in `uv.lock`, using the
  `install-constraints.txt` file from the release tag;
- runs `quarry install`;
- restarts the daemon service and checks that its process ID changed;
- waits for the new daemon to report ready.

If the restart does not take effect, or the daemon never reports ready, the
installer exits with an error.

## HTTP API

`quarryd` exposes a REST API — every CLI/MCP operation is a thin client over
it. The CLI is the primary, documented way to drive quarry; the HTTP API is
there for scripting or a non-Claude integration that wants to talk to the
daemon directly. `quarry install` generates a self-signed CA for the managed
daemon, local or remote, so it's TLS even on loopback:

```bash
{ printf 'Authorization: Bearer '
  tr -d '\n' < ~/.punt-labs/quarry/data/default/serve.token
  echo; } |
  curl --cacert ~/.punt-labs/quarry/tls/ca.crt -H @- \
    "https://127.0.0.1:8420/v1/search?q=Q3+revenue"
```

The header is built on the pipe and read from stdin (`-H @-`), so the token is
never a command-line argument of any process (`curl`, `printf`, or `tr`):
other local users can read argv from the process table. Avoid
`"$(cat serve.token)"` and `-H "Authorization: Bearer $TOKEN"` for that reason.

Every `/v1` endpoint requires a Bearer token, loopback included. A local
install binds loopback-only and needs no key configured: `quarryd` mints a
fresh token at each start and writes it to `serve.token` (mode 0600) in its
data directory, where the CLI, MCP server, and hooks read it. Scripts read
the same file; a `401` after a daemon restart means the token rotated, so
re-read it. Only `/health` and `/ca.crt` are unauthenticated, so a client can
probe liveness and bootstrap TLS trust. A `--network` install refuses to
start without an operator key (`QUARRY_API_KEY`), which remote clients
present instead (see [Remote Server](#remote-server)). The full endpoint list
is generated at [docs/openapi.json](docs/openapi.json) (`make openapi`
regenerates it).
