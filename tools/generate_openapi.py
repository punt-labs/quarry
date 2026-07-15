"""Generate (or verify) the daemon's OpenAPI document from the live FastAPI app.

``python tools/generate_openapi.py`` writes ``docs/openapi.json``;
``--check`` fails if the committed file is stale (a CI drift guard).  The schema
is rendered deterministically (sorted keys) so the check is reproducible.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Self, final

from quarry.config import Settings
from quarry.daemon.app import build_app
from quarry.daemon.context import DaemonContext

_OUTPUT = Path("docs/openapi.json")


@final
class OpenApiDoc:
    """The daemon's rendered OpenAPI schema, built once from a fresh app."""

    _schema: dict[str, Any]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        # DaemonContext defers every engine resource, and ``openapi()`` only
        # introspects the route table, so no DB or ONNX model is loaded here.
        self._schema = build_app(DaemonContext(Settings.load())).openapi()
        return self

    def rendered(self) -> str:
        """Return the schema as deterministic, newline-terminated JSON."""
        return json.dumps(self._schema, indent=2, sort_keys=True) + "\n"

    def write(self, path: Path) -> None:
        """Write the rendered schema to *path*."""
        path.write_text(self.rendered())

    def is_current(self, path: Path) -> bool:
        """Return whether *path* already holds the rendered schema."""
        return path.exists() and path.read_text() == self.rendered()


def main(argv: list[str]) -> int:
    """Write ``docs/openapi.json``, or verify it under ``--check``."""
    doc = OpenApiDoc()
    if "--check" in argv:
        if doc.is_current(_OUTPUT):
            return 0
        print("docs/openapi.json is stale — run `make openapi`", file=sys.stderr)
        return 1
    doc.write(_OUTPUT)
    print(f"wrote {_OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
