"""Fixtures for the hook-integration regression suite (quarry-yndv).

These fixtures spawn the real ``quarry-hook`` and ``quarryd`` binaries so the
tests exercise the same code path a user hits, not a hermetic in-process
double.  The in-process fake was what let quarry-jzqw, quarry-ridg, and
quarry-871u ship in v3.2.0 unnoticed — this module exists so the fix beads
have a black-box acceptance bar.

Everything here is gated behind the ``hook_integration`` pytest marker
(registered in ``pyproject.toml``) and deselected from the default suite.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

import httpx
import pytest

try:
    import pwd as _pwd
except ImportError:  # pragma: no cover — POSIX-only; Windows falls back to Path.home()

    def _real_home() -> Path:
        """Return the operator's real home; falls back to ``Path.home()`` off POSIX."""
        return Path.home()

else:

    def _real_home() -> Path:
        """Return the operator's real home from the password DB, ignoring ``$HOME``."""
        try:
            return Path(_pwd.getpwuid(os.getuid()).pw_dir)
        except KeyError:
            # Minimal container images may lack a passwd entry for the UID;
            # fall back to Path.home() (which reads $HOME — overridden by the
            # hermetic conftest, but accepting that override beats crashing
            # fixture setup on a stripped-down CI image).
            return Path.home()


if TYPE_CHECKING:
    from collections.abc import Generator, Iterable


# ── Ephemeral quarryd ────────────────────────────────────────────────


@final
@dataclass(frozen=True, slots=True)
class EphemeralDaemon:
    """Coordinates for a live ``quarryd`` bound to loopback in a scratch tree.

    ``ca_cert_path`` is ``None`` in the current fixture; the mission spec
    named ``--tls`` but the regressions under test (jzqw/ridg/871u) do not
    hinge on TLS transport, so the fixture binds plaintext loopback to keep
    the cert-material bootstrap out of the failure path.  A follow-up may
    restore TLS once the fix beads land and the tests turn green.
    """

    host: str
    port: int
    api_token: str
    ca_cert_path: str | None
    data_dir: Path
    env: dict[str, str]

    @property
    def base_url(self) -> str:
        """Return the ``http://host:port`` base URL for HTTP-API probes."""
        return f"http://{self.host}:{self.port}"


def _read_port_file(path: Path, timeout_s: float) -> int:
    """Return the port from ``serve.port``, polling until the daemon writes it.

    Spawning ``quarryd --port 0`` lets the OS pick the port; the daemon then
    atomically renames its ``serve.port`` sidecar into place with the bound
    value.  Polling that file replaces the old choose-a-port-then-start-daemon
    handshake, which had a race: any other process could grab the picked port
    between our ``close()`` and the daemon's ``bind()``.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            return int(path.read_text().strip())
        except (FileNotFoundError, ValueError):
            time.sleep(0.1)
    msg = f"quarryd did not publish {path} within {timeout_s:.1f}s"
    raise RuntimeError(msg)


def _hf_home() -> str | None:
    """Return an ``HF_HOME`` value that keeps the daemon hermetic.

    An explicit ``$HF_HOME`` in the parent env wins — operators and CI can
    force a specific cache location. Otherwise reuse the operator's real
    cache only if it already exists on disk; without that guard a first-run
    test would trigger a ~200 MB download into the operator's real home,
    violating DES-047 hermeticity. When neither applies, return ``None`` so
    the caller leaves ``HF_HOME`` unset and any download stays under the
    sandbox ``HOME``.
    """
    if hf_home := os.environ.get("HF_HOME"):
        return hf_home
    cache_dir = _real_home() / ".cache" / "huggingface"
    if cache_dir.exists():
        return str(cache_dir)
    return None


def _daemon_env(root: Path, log_dir: Path, api_key: str) -> dict[str, str]:
    """Build the env for the daemon subprocess.

    Quarry paths are isolated to *root* (``QUARRY_ROOT`` + ``QUARRY_LOG_DIR``
    override every home-derived path :mod:`quarry.config` resolves). The
    operator's HuggingFace cache is reused via ``HF_HOME`` when it exists —
    otherwise ``quarryd`` would re-download the ~200 MB ONNX embedding model
    every session and ``/health`` would never reach ``ready`` inside the 30s
    poll window. When no operator cache is available, ``HF_HOME`` is left
    unset so any download lands under the sandbox ``HOME`` and hermeticity
    holds — see :func:`_hf_home`.
    """
    env = os.environ.copy()
    env["HOME"] = str(root)
    env["QUARRY_ROOT"] = str(root / "quarry")
    env["QUARRY_LOG_DIR"] = str(log_dir)
    env["QUARRY_API_KEY"] = api_key
    env["TMPDIR"] = str(root / "tmp")
    if (hf_home := _hf_home()) is not None:
        env["HF_HOME"] = hf_home
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    return env


def _wait_ready(base_url: str, api_token: str, timeout_s: float) -> None:
    """Poll ``/health`` until the daemon reports ``ready``, else raise."""
    deadline = time.monotonic() + timeout_s
    headers = {"Authorization": f"Bearer {api_token}"}
    last: str = "no attempt made"
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/health", headers=headers, timeout=2.0)
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 200 and resp.json().get("state") == "ready":
                return
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
        time.sleep(0.25)
    msg = f"quarryd did not become ready within {timeout_s:.1f}s: {last}"
    raise RuntimeError(msg)


@pytest.fixture(scope="session")
def ephemeral_daemon(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[EphemeralDaemon]:
    """Spawn a real ``quarryd`` bound to loopback in a scratch tree.

    Session-scoped: bringing the engine up (ONNX warm, LanceDB open) costs
    several seconds, and no test in this suite needs a fresh DB.  Each test
    that mutates data namespaces its writes under a per-test collection so
    the shared daemon stays a stateless substrate.
    """
    binary = shutil.which("quarryd")
    if binary is None:
        pytest.skip("quarryd binary not on PATH (install the wheel first)")

    root = tmp_path_factory.mktemp("ephemeral-daemon-home")
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    api_key = "test-key-" + os.urandom(8).hex()
    env = _daemon_env(root, log_dir, api_key)

    stderr = (log_dir / "quarryd.stderr").open("wb")
    stdout = (log_dir / "quarryd.stdout").open("wb")
    try:
        proc = subprocess.Popen(
            [binary, "--host", "127.0.0.1", "--port", "0"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
        )
    except BaseException:
        stdout.close()
        stderr.close()
        raise

    try:
        # ``$QUARRY_ROOT/default/serve.port`` is the daemon's post-bind port
        # sidecar (run_dir.PortFile).  Reading it — instead of picking a port
        # ourselves and passing ``--port <n>`` — closes the pick→bind race that
        # made this fixture flaky under session-scoped reuse.
        port_file = Path(env["QUARRY_ROOT"]) / "default" / "serve.port"
        port = _read_port_file(port_file, timeout_s=15.0)
        daemon = EphemeralDaemon(
            host="127.0.0.1",
            port=port,
            api_token=api_key,
            ca_cert_path=None,
            data_dir=Path(env["QUARRY_ROOT"]),
            env=env,
        )
        _wait_ready(daemon.base_url, api_key, timeout_s=30.0)
        yield daemon
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        stderr.close()
        stdout.close()


# ── quarry-hook subprocess ───────────────────────────────────────────


@final
@dataclass(frozen=True, slots=True)
class HookRun:
    """The observable outcome of one ``quarry-hook <event>`` invocation."""

    stdout: str
    stderr: str
    log_lines: tuple[str, ...]
    exit_code: int
    log_path: Path

    @classmethod
    def from_process(
        cls, completed: subprocess.CompletedProcess[str], log_path: Path
    ) -> Self:
        """Build from a ``subprocess.run`` result + the resolved log-file path."""
        lines = tuple(log_path.read_text().splitlines()) if log_path.exists() else ()
        return cls(
            stdout=completed.stdout,
            stderr=completed.stderr,
            log_lines=lines,
            exit_code=completed.returncode,
            log_path=log_path,
        )

    def grep(self, needle: str) -> tuple[str, ...]:
        """Return every log line that contains *needle* (substring match)."""
        return tuple(line for line in self.log_lines if needle in line)


@final
@dataclass(frozen=True, slots=True)
class HookInvoker:
    """Run ``quarry-hook <event>`` with a JSON payload piped on stdin.

    Each call resolves its own ``QUARRY_LOG_DIR`` under *tmp_root* and returns
    a :class:`HookRun` whose ``log_lines`` are read from
    ``$QUARRY_LOG_DIR/quarry.log`` post-exit.
    """

    _tmp_root: Path
    _extra_env: dict[str, str]

    @classmethod
    def build(cls, tmp_root: Path, extra_env: dict[str, str] | None = None) -> Self:
        """Return an invoker rooted at *tmp_root* with optional extra env."""
        return cls(_tmp_root=tmp_root, _extra_env=dict(extra_env or {}))

    def run(
        self,
        event: str,
        payload: object,
        *,
        env_overrides: dict[str, str] | None = None,
        timeout_s: float = 20.0,
    ) -> HookRun:
        """Invoke ``quarry-hook <event>`` and return the observed outcome."""
        log_dir = self._tmp_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        env = self._compose_env(log_dir, env_overrides)
        cmd = self._resolve_command(event)
        completed = subprocess.run(
            cmd,
            input=json.dumps(payload),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return HookRun.from_process(completed, log_dir / "quarry.log")

    def _compose_env(
        self, log_dir: Path, env_overrides: dict[str, str] | None
    ) -> dict[str, str]:
        """Return the child env: base + fixture defaults + per-call overrides."""
        env = os.environ.copy()
        env["HOME"] = str(self._tmp_root)
        env["QUARRY_ROOT"] = str(self._tmp_root / "quarry")
        env["QUARRY_LOG_DIR"] = str(log_dir)
        env["TMPDIR"] = str(self._tmp_root / "tmp")
        (self._tmp_root / "tmp").mkdir(parents=True, exist_ok=True)
        env.update(self._extra_env)
        if env_overrides:
            env.update(env_overrides)
        return env

    @staticmethod
    def _resolve_command(event: str) -> list[str]:
        """Return the argv for ``event`` — module path from the current interpreter.

        Using ``[sys.executable, "-m", "quarry._hook_entry", event]`` guarantees
        the tests exercise the ``quarry`` source under test (whichever venv or
        PYTHONPATH resolves ``import quarry``), not whatever ``quarry-hook``
        binary happens to sit on ``$PATH`` from a prior install.  The demo gate
        exercises the installed binary; the test suite exercises the source.
        """
        return [sys.executable, "-m", "quarry._hook_entry", event]


@pytest.fixture()
def hook_subprocess(tmp_path: Path) -> HookInvoker:
    """Return a :class:`HookInvoker` scoped to this test's tmp_path.

    Each call to ``HookInvoker.run(event, payload)`` returns a :class:`HookRun`
    with stdout, stderr, exit code, and every line written to
    ``$QUARRY_LOG_DIR/quarry.log`` during the subprocess's lifetime.
    """
    return HookInvoker.build(tmp_path)


# ── Fixture loader ───────────────────────────────────────────────────


FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "hook_payloads"


def load_payload(relpath: str) -> dict[str, object]:
    """Return the parsed JSON payload from ``fixtures/hook_payloads/<relpath>``."""
    data = json.loads((FIXTURES_ROOT / relpath).read_text())
    if not isinstance(data, dict):
        msg = f"fixture {relpath} is not a JSON object"
        raise TypeError(msg)
    return data


def websearch_payload_ids() -> Iterable[str]:
    """Return the three websearch fixture stem names for parametrization."""
    return ("pre_2026_05", "2026_05_markdown", "unknown_shape")
