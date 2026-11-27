"""Guards the Ethos handoff checks: a subagent stop must stay fast and bounded."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CHECKS_DIR = ROOT / ".punt-labs/ethos/checks"
HANDOFF_CHECK_TIMEOUT_SECONDS = 120


def _load_profile() -> dict[str, object]:
    loaded = yaml.safe_load((CHECKS_DIR / "quarry-default.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _handoff_checks() -> list[dict[str, object]]:
    checks = _load_profile()["checks"]
    assert isinstance(checks, list)
    return [c for c in checks if c["stage"] == "handoff"]


def _seconds(duration: str) -> int:
    scale = {"s": 1, "m": 60}
    return int(duration[:-1]) * scale[duration[-1]]


def test_handoff_stage_does_not_run_the_full_gate() -> None:
    offenders = [
        c["name"]
        for c in _handoff_checks()
        if c.get("adapter") == "make" and c.get("target") == "check"
    ]
    assert offenders == []


def test_handoff_checks_are_scoped_to_python_paths() -> None:
    handoff = _handoff_checks()
    assert handoff, "the profile must keep a handoff check"
    for check in handoff:
        files = check["files"]
        assert isinstance(files, list), check["name"]
        assert all(str(p).endswith((".py", ".pyi")) for p in files)


def test_handoff_timeouts_are_bounded() -> None:
    for check in _handoff_checks():
        assert "timeout" in check, check["name"]
        assert _seconds(str(check["timeout"])) <= HANDOFF_CHECK_TIMEOUT_SECONDS


def test_handoff_snapshot_covers_exactly_the_handoff_checks() -> None:
    snapshot = json.loads((CHECKS_DIR / "snapshots/quarry-default.yaml").read_text())
    assert set(snapshot["checks"]) == {str(c["name"]) for c in _handoff_checks()}
