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

_ENGINE = (
    "lancedb",
    "onnxruntime",
    "pyarrow",
    "quarry.db",
    "quarry.ingestion",
    "quarry.retrieval",
    "quarry.sync",
)

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
    code = (
        "import sys;"
        "sys.modules['lancedb'] = None;"
        "sys.modules['onnxruntime'] = None;"
        "import quarry;"
        "engine = ('pyarrow', 'quarry.db', 'quarry.ingestion',"
        "          'quarry.retrieval', 'quarry.sync');"
        "loaded = [m for m in engine if m in sys.modules];"
        "assert not loaded, loaded;"
        "_ = (quarry.QuarryClient, quarry.TargetResolver, quarry.ClientConfig,"
        "     quarry.QuarryError, quarry.QuarryConnectionError, quarry.HttpError,"
        "     quarry.TaskOutcome);"
        "loaded = [m for m in engine if m in sys.modules];"
        "assert not loaded, loaded;"
        "print('engine-free')"
    )
    result = _run(code)
    assert result.returncode == 0, result.stderr
    assert "engine-free" in result.stdout


def test_bare_import_stays_lazy() -> None:
    """A bare ``import quarry`` pulls in neither pydantic nor ``quarry.client``.

    This codifies the hook-budget contract: the client tier (pydantic + httpx)
    loads only on first client-name access, so a future eager
    ``from quarry.client import ...`` in ``__init__`` fails this test instead of
    silently regressing the ``quarry-hook`` fast path.
    """
    code = (
        "import sys, quarry;"
        "hot = [m for m in ('pydantic', 'quarry.client') if m in sys.modules];"
        "assert not hot, hot;"
        "quarry.QuarryClient;"
        "assert 'quarry.client' in sys.modules;"
        "print('lazy')"
    )
    result = _run(code)
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
