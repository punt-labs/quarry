"""Tests for the top-level ``quarry`` package — the thin-client library surface.

The package must satisfy DES-031 invariant I1: ``import quarry`` loads **zero
engine** (no lancedb/onnxruntime/pyarrow, no ``quarry.db``/``ingestion``/
``retrieval``/``sync``), exposes only the client surface, and the deleted engine
names raise ``AttributeError`` rather than aliasing back to the engine. A fourth
guard keeps the loader lazy so ``import quarry`` stays cheap on the hot
``quarry-hook`` path (pydantic/httpx load only on first client-name access).

The ``sys.modules`` assertions run in a fresh interpreter: the main test process
already has the engine loaded, so an in-process check could not detect a hidden
import. Engine sabotage (poisoning ``lancedb``/``onnxruntime`` to ``None``) makes
any accidental engine import fail loudly instead of passing true-by-luck.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import quarry

# The engine modules a thin ``import quarry`` must never load. ``_POISONED`` is
# the subset set to ``None`` in a fresh interpreter so a stray ``import`` of them
# raises ``ImportError``; the rest are checked by absence from ``sys.modules``.
# One canonical list drives both — no second, drifting copy.
_ENGINE = (
    "lancedb",
    "onnxruntime",
    "pyarrow",
    "quarry.db",
    "quarry.ingestion",
    "quarry.retrieval",
    "quarry.sync",
)
_POISONED = ("lancedb", "onnxruntime")

_REMOVED_ENGINE_NAMES = (
    "Database",
    "get_db",
    "ChunkSearch",
    "ingest_content",
    "ingest_document",
    "ingest_url",
)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter and return the completed process."""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )


def test_import_quarry_is_engine_free_under_sabotage() -> None:
    """``import quarry`` and every public name load no engine, even poisoned.

    Poisoning ``lancedb``/``onnxruntime`` to ``None`` turns any accidental
    ``import lancedb`` in the import or attribute-access path into an
    ``ImportError``, so a future re-target back to the engine fails here.
    """
    program = f"""
import sys
poison = {_POISONED!r}
for _m in poison:
    sys.modules[_m] = None  # any real import of these now raises ImportError
import quarry
engine = {_ENGINE!r}
check = [m for m in engine if m not in poison]
loaded = [m for m in check if m in sys.modules]
assert not loaded, loaded
_ = tuple(getattr(quarry, n) for n in quarry.__all__ if n != "__version__")
loaded = [m for m in check if m in sys.modules]
assert not loaded, loaded
print("engine-free")
"""
    result = _run(program)
    assert result.returncode == 0, result.stderr
    assert "engine-free" in result.stdout


def test_mcp_server_is_engine_free_under_sabotage() -> None:
    """``import quarry.mcp_server`` and its tools load no engine, even poisoned.

    ``quarry mcp`` is a client-tier FastMCP server (DES-031 v2.2 R1): importing
    the module, building ``McpTools``, and registering every tool must pull in
    zero engine, so a stray ``import lancedb`` on the MCP path fails here.  A
    tool call with a down client returns a clean error string — proving the
    fail-closed boundary loads no engine either.
    """
    program = f"""
import sys
poison = {_POISONED!r}
for _m in poison:
    sys.modules[_m] = None  # any real import of these now raises ImportError
import quarry.mcp_server as mcp_server
from mcp.server.fastmcp import FastMCP

engine = {_ENGINE!r}
check = [m for m in engine if m not in poison]
loaded = [m for m in check if m in sys.modules]
assert not loaded, loaded

# Registering every tool on a fresh server must not import the engine.
server = FastMCP("sabotage")
mcp_server.McpTools().register(server)

# A tool call with a down client returns an error string, not an engine import.
def _down():
    raise RuntimeError("daemon down")
result = mcp_server.McpTools(connect=_down).status()
assert result.startswith("Error:"), result

loaded = [m for m in check if m in sys.modules]
assert not loaded, loaded
print("engine-free")
"""
    result = _run(program)
    assert result.returncode == 0, result.stderr
    assert "engine-free" in result.stdout


def test_bare_import_stays_lazy() -> None:
    """A bare ``import quarry`` pulls in neither pydantic nor ``quarry.client``.

    This codifies the hook-budget contract: the client tier (pydantic + httpx)
    loads only on first client-name access, so a future eager
    ``from quarry.client import ...`` in ``__init__`` fails this test instead of
    silently regressing the ``quarry-hook`` fast path.
    """
    program = """
import sys
import quarry
hot = [m for m in ("pydantic", "quarry.client") if m in sys.modules]
assert not hot, hot
quarry.QuarryClient
assert "quarry.client" in sys.modules
print("lazy")
"""
    result = _run(program)
    assert result.returncode == 0, result.stderr
    assert "lazy" in result.stdout


def test_public_surface_matches_all() -> None:
    """Every ``__all__`` name resolves; the surface is exactly the eight names."""
    assert quarry.__all__ == [
        "ClientConfig",
        "HttpError",
        "QuarryClient",
        "QuarryConnectionError",
        "QuarryError",
        "TargetResolver",
        "TaskOutcome",
        "__version__",
    ]
    for name in quarry.__all__:
        assert getattr(quarry, name) is not None, name


@pytest.mark.parametrize("name", _REMOVED_ENGINE_NAMES)
def test_removed_engine_name_raises(name: str) -> None:
    """Each deleted engine export raises ``AttributeError`` — removed, not aliased."""
    with pytest.raises(AttributeError):
        getattr(quarry, name)
