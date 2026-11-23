"""Exercise the installed-wheel search check without a model or package install."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_CANARY = Path(__file__).resolve().parents[1] / "scripts" / "check-wheel-canary.sh"
_NAME = "wheel-canary.md"
_BODY = "The wheel canary lives in the blue observatory."


@pytest.mark.parametrize(
    ("search_output", "remember_status", "expected", "attempts"),
    [
        (json.dumps([{"document_name": _NAME, "text": _BODY}]), 0, "", 2),
        ("[]", 0, "did not return the indexed document", 30),
        ("invalid JSON", 0, "Invalid search results", 1),
        ('[{"document_name": "other", "text": null}]', 0, "Invalid search results", 1),
        (
            json.dumps([{"document_name": "wrong", "text": _BODY}]),
            0,
            "did not return",
            30,
        ),
        (
            json.dumps([{"document_name": _NAME, "text": "wrong"}]),
            0,
            "did not return",
            30,
        ),
        ("[]", 2, "remember refused", 0),
    ],
)
def test_wheel_canary_handles_search_results(
    tmp_path: Path,
    search_output: str,
    remember_status: int,
    expected: str,
    attempts: int,
) -> None:
    """Distinguish a retryable indexing delay from definite CLI or schema errors."""
    quarry = tmp_path / "quarry"
    quarry.write_text(
        """#!/usr/bin/env bash
set -eu
case "$*" in
  'remember --name wheel-canary.md --collection wheel-smoke')
    read -r body
    [[ "$body" == 'The wheel canary lives in the blue observatory.' ]]
    if [[ "$REMEMBER_STATUS" != 0 ]]; then echo 'remember refused' >&2; fi
    exit "$REMEMBER_STATUS"
    ;;
  '--json find --collection wheel-smoke --limit 1 blue observatory')
    if [[ "$DELAY_FIRST" == 1 && ! -e requests ]]; then
      printf '[]\n'
    else
      printf '%s\n' "$SEARCH_OUTPUT"
    fi
    printf '1\n' >>requests
    ;;
  *) exit 2 ;;
esac
"""
    )
    quarry.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    result = subprocess.run(
        ["bash", str(_CANARY), sys.executable, str(quarry), "blue observatory"],
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
            "HOME": str(tmp_path),
            "SEARCH_OUTPUT": search_output,
            "REMEMBER_STATUS": str(remember_status),
            "DELAY_FIRST": "0" if expected else "1",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert (result.returncode == 0) == (not expected), result.stderr
    assert expected in result.stderr
    log = tmp_path / "requests"
    assert (log.read_text().count("\n") if log.exists() else 0) == attempts
