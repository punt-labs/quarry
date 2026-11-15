"""Logic tests for plugin/hooks/session-end.sh using a mock ``quarry-hook``.

SessionEnd runs in Claude Code's teardown window: a hook still running when the
harness exits is cancelled. The hook must therefore return promptly and hand the
slow transcript ingest to a detached job.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "plugin" / "hooks" / "session-end.sh"
PARENT_DEADLINE_S = 2.0
JOB_DEADLINE_S = 15.0
PAYLOAD = '{"session_id": "abc", "transcript_path": "/tmp/t.jsonl"}'


def _mock_quarry_hook(
    bin_dir: Path, log: Path, delay_s: float, pgid_log: Path | None = None
) -> None:
    script = bin_dir / "quarry-hook"
    pgid_line = f'ps -o pgid= -p $$ > "{pgid_log}"\n' if pgid_log else ""
    script.write_text(
        "#!/bin/sh\n"
        + pgid_line
        + f"sleep {delay_s}\n"
        + f'printf "%s\\n" "$1" >> "{log}"\n'
        + f'cat >> "{log}"\n'
        + f'printf "\\n" >> "{log}"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


def _run_hook(
    home: Path, path_dirs: list[Path]
) -> tuple[subprocess.CompletedProcess[str], float]:
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join([*(str(d) for d in path_dirs), "/usr/bin", "/bin"]),
    }
    start = time.monotonic()
    result = subprocess.run(
        ["bash", str(HOOK)],
        input=PAYLOAD,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return result, time.monotonic() - start


def _wait_for(log: Path) -> str:
    deadline = time.monotonic() + JOB_DEADLINE_S
    while time.monotonic() < deadline:
        if log.exists() and log.read_text().count("\n") >= 2:
            return log.read_text()
        time.sleep(0.1)
    return log.read_text() if log.exists() else ""


class TestSessionEndHook:
    def test_parent_returns_promptly_on_slow_ingest(self, tmp_path: Path) -> None:
        log = tmp_path / "calls.log"
        _mock_quarry_hook(tmp_path, log, delay_s=5)
        result, elapsed = _run_hook(tmp_path, [tmp_path])
        assert result.returncode == 0
        assert elapsed < PARENT_DEADLINE_S

    def test_detached_job_gets_input_and_runs_session_end(self, tmp_path: Path) -> None:
        log = tmp_path / "calls.log"
        _mock_quarry_hook(tmp_path, log, delay_s=1)
        result, _ = _run_hook(tmp_path, [tmp_path])
        assert result.returncode == 0
        lines = _wait_for(log).splitlines()
        assert lines[0] == "session-end"
        assert lines[1] == PAYLOAD

    def test_detached_job_runs_in_its_own_process_group(self, tmp_path: Path) -> None:
        log = tmp_path / "calls.log"
        pgid_log = tmp_path / "pgid.log"
        _mock_quarry_hook(tmp_path, log, delay_s=1, pgid_log=pgid_log)
        parent_pgid = os.getpgid(0)
        result, _ = _run_hook(tmp_path, [tmp_path])
        assert result.returncode == 0
        _wait_for(log)
        job_pgid = int(pgid_log.read_text().strip())
        assert job_pgid != parent_pgid

    def test_kill_switch_short_circuits(self, tmp_path: Path) -> None:
        log = tmp_path / "calls.log"
        _mock_quarry_hook(tmp_path, log, delay_s=0)
        (tmp_path / ".punt-hooks-kill").write_text("")
        result, _ = _run_hook(tmp_path, [tmp_path])
        assert result.returncode == 0
        time.sleep(0.5)
        assert not log.exists()

    def test_missing_quarry_hook_exits_zero(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        result, _ = _run_hook(tmp_path, [empty])
        assert result.returncode == 0
