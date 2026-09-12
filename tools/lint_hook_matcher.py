"""Guard: plugin/hooks/hooks.json's PostToolUse matcher must cover native tools.

A substring lint over the raw JSON is too weak: the matcher can still require
the retired ``plugin_quarry`` prefix (and the substring still appears), leaving
native ``mcp__quarry__*`` / ``mcp__quarry-dev__*`` tool calls to bypass
``suppress-output.sh``. It is also brittle to equivalent regex spellings such
as ``(-dev|-proxy)``. Parse the JSON, compile the regex, and check it accepts
concrete sample tool names.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Self, final

_HOOKS_JSON = Path(__file__).resolve().parent.parent / "plugin" / "hooks" / "hooks.json"

# Native namespaces the matcher MUST admit — otherwise every native quarry tool
# call skips suppress-output.sh, which is the failure quarry-ydym exists to stop.
_REQUIRED: tuple[str, ...] = (
    "mcp__quarry__status",
    "mcp__quarry__find",
    "mcp__quarry-dev__status",
    "mcp__quarry-dev__find",
)

# Legacy proxy spellings the matcher MAY still admit — the plugin_quarry
# namespace has been retired, so their absence is a soft note, not a failure.
_LEGACY: tuple[str, ...] = (
    "mcp__plugin_quarry_quarry__status",
    "mcp__plugin_quarry-dev_quarry__status",
)


@final
class PostToolUseMatcher:
    """The PostToolUse matcher that gates ``suppress-output.sh``, compiled."""

    _raw: str
    _pattern: re.Pattern[str]

    def __new__(cls, raw: str) -> Self:
        self = super().__new__(cls)
        self._raw = raw
        self._pattern = re.compile(raw)
        return self

    @classmethod
    def load(cls, hooks_json: Path) -> Self:
        """Return the matcher on the PostToolUse entry that runs suppress-output.sh."""
        doc = json.loads(hooks_json.read_text(encoding="utf-8"))
        for entry in doc["hooks"]["PostToolUse"]:
            for hook in entry["hooks"]:
                if "suppress-output" in hook.get("command", ""):
                    return cls(entry["matcher"])
        raise ValueError(
            f"{hooks_json}: no PostToolUse entry references suppress-output.sh",
        )

    @property
    def raw(self) -> str:
        """Return the matcher regex as it appears in hooks.json."""
        return self._raw

    def covers(self, tool: str) -> bool:
        """Return whether the matcher admits *tool* as a full match."""
        return self._pattern.fullmatch(tool) is not None

    def missing_from(self, samples: tuple[str, ...]) -> tuple[str, ...]:
        """Return the samples the matcher does NOT admit, in input order."""
        return tuple(s for s in samples if not self.covers(s))


def main(argv: list[str]) -> int:
    """Fail if the matcher drops native mcp__quarry__ / mcp__quarry-dev__ coverage."""
    del argv  # No flags — this is a pass/fail gate, invoked by the Makefile.
    matcher = PostToolUseMatcher.load(_HOOKS_JSON)
    missed = matcher.missing_from(_REQUIRED)
    if missed:
        print(
            f"plugin/hooks/hooks.json PostToolUse matcher {matcher.raw!r} does not "
            f"admit required native tool names: {', '.join(missed)}",
            file=sys.stderr,
        )
        return 1
    legacy_missed = matcher.missing_from(_LEGACY)
    if legacy_missed:
        print(
            f"plugin/hooks/hooks.json PostToolUse matcher {matcher.raw!r} no longer "
            f"admits retired legacy proxy tool names: {', '.join(legacy_missed)} "
            f"(informational; the plugin_quarry namespace is retired)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
