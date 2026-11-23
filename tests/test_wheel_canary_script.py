"""Exercise installed-wheel smoke failures without model or package downloads."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_NAME = "wheel-canary.md"
_BODY = "The wheel canary lives in the blue observatory."
_GOOD = json.dumps([{"document_name": _NAME, "text": _BODY}])


@dataclass(frozen=True, slots=True)
class CanaryCase:
    """Inputs and expected diagnostic for a wheel canary invocation."""

    hits: str = _GOOD
    failure: str = ""
    expected: str = ""


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(CanaryCase(), id="success"),
        pytest.param(CanaryCase("[]", expected="did not return"), id="missing"),
        pytest.param(
            CanaryCase("invalid JSON", expected="Invalid search results"), id="invalid"
        ),
        pytest.param(
            CanaryCase(
                '[{"document_name": "other", "text": null}]',
                expected="Invalid search results",
            ),
            id="malformed-result",
        ),
        pytest.param(
            CanaryCase(failure="nul", expected="Invalid search results"), id="nul"
        ),
        pytest.param(
            CanaryCase(
                json.dumps([{"document_name": "wrong", "text": _BODY}]),
                expected="did not return",
            ),
            id="wrong-name",
        ),
        pytest.param(
            CanaryCase(
                json.dumps([{"document_name": _NAME, "text": "wrong"}]),
                expected="did not return",
            ),
            id="wrong-body",
        ),
        pytest.param(
            CanaryCase(failure="remember", expected="remember refused"),
            id="remember-refused",
        ),
        pytest.param(
            CanaryCase(failure="task", expected="remember task failed"),
            id="task-failed",
        ),
    ],
)
def test_wheel_canary_handles_search_results(tmp_path: Path, case: CanaryCase) -> None:
    """Find uses one query argument and only runs after the remember task completes."""
    quarry = tmp_path / "quarry"
    quarry.write_text(
        """#!/usr/bin/env bash
set -eu
case "$2" in
  remember)
    [[ "$#" == 6 && "$3" == --name && "$4" == wheel-canary.md ]]
    [[ "$5" == --collection && "$6" == wheel-smoke ]]
    read -r body
    [[ "$body" == 'The wheel canary lives in the blue observatory.' ]]
    if [[ "$FAILURE" == remember ]]; then echo 'remember refused' >&2; exit 2; fi
    printf '{"task_id":"canary-accepted"}\n'
    ;;
  find)
    [[ "$#" == 7 && "$1" == --json && "$3" == --collection && "$4" == wheel-smoke ]]
    [[ "$5" == --limit && "$6" == 1 && "$7" == 'blue observatory' ]]
    [[ -e task-completed && "$FAILURE" != task ]]
    printf '%s' "$SEARCH_OUTPUT"
    if [[ "$FAILURE" == nul ]]; then printf '\\000'; fi
    ;;
  *) exit 2 ;;
esac
"""
    )
    quarry.chmod(0o755)
    python = tmp_path / "python"
    python.write_text(
        f"#!{sys.executable}\n"
        """import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

if sys.argv[1:] != ['-I', '-', 'remember.json']:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])

def task_status(task_id):
    assert task_id == 'canary-accepted'
    pending = Path('task-pending')
    if not pending.exists():
        pending.touch()
        return SimpleNamespace(status='running')
    if os.environ['FAILURE'] == 'task':
        return SimpleNamespace(status='failed', error='bad document')
    Path('task-completed').touch()
    return SimpleNamespace(status='completed')

sys.modules['quarry'] = SimpleNamespace(
    TargetResolver=SimpleNamespace(
        connect=lambda: SimpleNamespace(task_status=task_status)
    )
)
time.sleep = lambda _: None
sys.argv = sys.argv[2:]
exec(compile(sys.stdin.read(), '<task polling>', 'exec'))
"""
    )
    python.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            str(_SCRIPTS / "check-wheel-canary.sh"),
            str(python),
            str(quarry),
            "blue observatory",
        ],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "SEARCH_OUTPUT": case.hits,
            "FAILURE": case.failure,
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert (result.returncode == 0) == (not case.expected), result.stderr
    assert case.expected in result.stderr


def test_wheel_smoke_reaches_build_after_tmpdir_is_deleted(tmp_path: Path) -> None:
    """A stale direnv TMPDIR must not stop the smoke before the build."""
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\n[ "$1 $2" = "--no-config build" ] && exit 34\nexit 1\n')
    uv.chmod(0o755)
    result = subprocess.run(
        ["bash", str(_SCRIPTS / "test-wheel.sh")],
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path / "deleted"),
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 34, result.stderr
