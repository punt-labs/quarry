"""Make one directory the only place quarry puts its data and logs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def home_only(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Make *home* the one place quarry puts its data and logs.

    The suite redirects ``QUARRY_ROOT`` and ``QUARRY_LOG_DIR``, which win over the
    home directory; a test about the home-derived paths clears them.
    """
    monkeypatch.setattr(Path, "home", lambda: home)
    for name in ("QUARRY_ROOT", "QUARRY_LOG_DIR", "LANCEDB_PATH"):
        monkeypatch.delenv(name, raising=False)
