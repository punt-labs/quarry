"""Tests for the daemon-log diagnostics under ``scripts/``.

Older installations also left ``quarry-stderr.log``, ``quarry.log``, and
``quarry-stdout.log``. Drive diagnostics against synthetic logs and assert
they report errors, always exit 0, handle missing or empty directories, and
never report a false clean when a log is unreadable.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LOGS_ERRORS = REPO_ROOT / "scripts" / "logs-errors.sh"
LOGS_TAIL = REPO_ROOT / "scripts" / "logs-tail.sh"

# A synthetic stderr log carrying two of the real signals the daemon emits: the
# broken-watch line (the incident that motivated the target) and the historical
# LanceDB fd exhaustion. The INFO line is a benign control that must NOT count.
FIXTURE_STDERR = (
    "2026-07-30 12:00:00 [INFO] quarry.sync: Ingested /x/a.py\n"
    "2026-07-30 12:00:01 Watch index failed for a.py: File not found: /x/a.py\n"
    "2026-07-30 12:00:02 LanceError(IO): Too many open files (os error 24)\n"
)


def _run(
    log_dir: Path, log_lines: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Invoke the script against *log_dir* via the LOG_DIR override.

    LOG_DIR points the scan at a fixture dir so the test never touches the live
    ``~/.punt-labs/quarry/logs``. LOG_LINES, when given, sets the recent-lines
    tail (and exercises the non-integer fallback when passed junk).
    """
    env = {**os.environ, "LOG_DIR": str(log_dir)}
    if log_lines is not None:
        env["LOG_LINES"] = log_lines
    return subprocess.run(
        ["bash", str(LOGS_ERRORS)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _write_log(log_dir: Path, name: str, text: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / name).write_text(text)


def test_reports_known_errors_and_exits_zero(tmp_path: Path) -> None:
    """A fixture with known error lines is summarized, and the script exits 0.

    Exit 0 is load-bearing: the target is a diagnostic like ``make report``, not
    a gate. Finding errors must not fail the caller.
    """
    logs = tmp_path / "logs"
    _write_log(logs, "quarry-stderr.log", FIXTURE_STDERR)
    result = _run(logs, log_lines="10")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    out = result.stdout
    assert "Watch index failed:" in out, "per-signal summary must be present"
    assert "Too many open files / EMFILE:" in out
    # Two distinct lines carry a signal; the INFO line carries none.
    assert "2 error lines found" in out
    # The recent-lines block echoes the actual matching lines back.
    assert "Watch index failed for a.py" in out
    assert "Too many open files" in out


def test_benign_log_reports_no_errors(tmp_path: Path) -> None:
    """A log with only INFO lines reports 'no errors found' and exits 0."""
    logs = tmp_path / "logs"
    _write_log(
        logs,
        "quarry.log",
        "2026-07-30 12:00:00 [INFO] quarry.sync: indexing complete for foo.py\n",
    )
    result = _run(logs)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "no errors found" in result.stdout
    assert "error lines found" not in result.stdout


def test_missing_log_dir_is_graceful(tmp_path: Path) -> None:
    """An absent log dir is the normal pre-daemon state: report it, exit 0.

    This is the exception-boundary discipline (bug class 2) for shell — the
    diagnostic must not error out when the daemon has never written a log.
    """
    missing = tmp_path / "never-created"
    result = _run(missing)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert f"no daemon logs at {missing}" in result.stdout


def test_empty_log_dir_is_graceful(tmp_path: Path) -> None:
    """A log dir that exists but holds no ``*.log`` files exits 0 cleanly."""
    logs = tmp_path / "logs"
    logs.mkdir()
    result = _run(logs)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert f"no daemon logs at {logs}" in result.stdout


def test_non_integer_log_lines_falls_back(tmp_path: Path) -> None:
    """A non-integer LOG_LINES warns and falls back to 40, never crashes tail.

    LOG_LINES feeds ``tail -n``; junk would abort it. The script must warn and
    continue (exit 0), producing its normal report.
    """
    logs = tmp_path / "logs"
    _write_log(logs, "quarry-stderr.log", FIXTURE_STDERR)
    result = _run(logs, log_lines="not-a-number")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "is not a non-negative integer" in result.stderr
    assert "2 error lines found" in result.stdout


def test_rotated_logs_are_excluded(tmp_path: Path) -> None:
    """Rotated ``quarry.log.1`` files are not scanned — only live ``*.log``.

    The glob targets ``*.log``; ``quarry.log.1`` matches ``*.log.1``, not
    ``*.log``, so a signal that lives only in a rotated file is not counted.
    """
    logs = tmp_path / "logs"
    _write_log(logs, "quarry.log", "2026-07-30 12:00:00 [INFO] quarry.sync: ok\n")
    _write_log(logs, "quarry.log.1", "Watch index failed for old.py: File not found\n")
    result = _run(logs)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "no errors found" in result.stdout


def test_unreadable_log_never_reports_a_false_clean(tmp_path: Path) -> None:
    """An unreadable ``*.log`` must NOT yield a false 'no errors found'.

    grep exits 2 on a permission/IO read failure. The old ``|| true`` flattened
    that into the benign no-match path, so the tool printed 'no errors found'
    and exited 0 when it could not actually read the logs — a false clean, the
    exact failure this diagnostic exists to catch (CLAUDE.md bug class 2). The
    scan must surface the read failure, withhold the clean verdict, and still
    exit 0 (a diagnostic never gates).

    This also guards the SUBSHELL-propagation fix. The verdict is driven solely
    by the marker file the counting path (``count_signal``, run in a ``$()``
    subshell) appends to on a grep read failure — NOT by the glob-time
    readability count, which only phrases the message. If that failure signal
    stopped crossing the subshell boundary (e.g. reverting to a plain
    ``scan_failed=1`` shell variable, lost when the subshell exits), the marker
    would stay empty, the verdict would fall through to 'no errors found', and
    this test would fail. The per-pattern warning on stderr proves the counting
    path itself detected the failure.
    """
    if os.geteuid() == 0:
        pytest.skip("root bypasses chmod 000, so the unreadable path can't be forced")
    logs = tmp_path / "logs"
    _write_log(logs, "quarry-stderr.log", FIXTURE_STDERR)
    target = logs / "quarry-stderr.log"
    target.chmod(0o000)
    try:
        result = _run(logs)
    finally:
        target.chmod(0o644)  # restore so tmp_path cleanup can remove it
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "no errors found" not in result.stdout, "must not report a false clean"
    assert "log scan incomplete" in result.stdout, "the incomplete scan is surfaced"
    # The counting path (in its subshell) detected the read failure and named
    # the pattern — proof the signal originated there, not only at the glob
    # pre-check, and reached the parent verdict via the marker file.
    assert "log scan failed (grep exit" in result.stderr


def test_unreadable_log_dir_is_not_reported_absent(tmp_path: Path) -> None:
    """An unreadable log DIR must NOT be reported as 'no daemon logs'.

    A dir with mode 000 defeats the ``*.log`` glob — it expands to the literal
    pattern, every ``[ -f ]`` guard fails, and the old code fell through to
    ``no daemon logs at <dir>``, a FALSE absent (the same misleading-fallback
    class as an unreadable file, bug class 2). The scan must surface the
    unreadable dir distinctly and still exit 0 — a diagnostic never gates.
    """
    if os.geteuid() == 0:
        pytest.skip("root bypasses chmod 000, so the unreadable path can't be forced")
    logs = tmp_path / "logs"
    _write_log(logs, "quarry-stderr.log", FIXTURE_STDERR)
    logs.chmod(0o000)
    try:
        result = _run(logs)
    finally:
        logs.chmod(0o755)  # restore so tmp_path cleanup can remove it
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "no daemon logs" not in result.stdout, "must not claim absent"
    assert "present but unreadable" in result.stderr, "the unreadable dir is surfaced"


def test_recent_lines_streamed_and_tailed_on_large_log(tmp_path: Path) -> None:
    """The recent-lines block streams grep into tail and shows only the last N.

    ``recent_lines`` pipes grep straight into ``tail`` rather than buffering the
    whole match set into a variable — on a real daemon log the matches run to
    thousands of lines. This exercises that path with a large synthetic log and
    asserts only the last ``LOG_LINES`` matches survive, in order, exit 0.
    """
    logs = tmp_path / "logs"
    lines = "".join(f"ERROR event number {n}\n" for n in range(2000))
    _write_log(logs, "quarry-stderr.log", lines)
    result = _run(logs, log_lines="5")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "2000 error lines found" in result.stdout
    # Only the final 5 matches appear in the recent-lines block.
    assert "ERROR event number 1999" in result.stdout
    assert "ERROR event number 1995" in result.stdout
    assert "ERROR event number 1994" not in result.stdout
    assert "ERROR event number 0" not in result.stdout


def _run_tail(
    log_dir: Path, log_lines: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Invoke ``logs-tail.sh`` against *log_dir* via the LOG_DIR override."""
    env = {**os.environ, "LOG_DIR": str(log_dir)}
    if log_lines is not None:
        env["LOG_LINES"] = log_lines
    return subprocess.run(
        ["bash", str(LOGS_TAIL)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_logs_tail_prints_recent_lines(tmp_path: Path) -> None:
    """logs-tail prints the last LOG_LINES lines of the diagnostic log."""
    logs = tmp_path / "logs"
    _write_log(logs, "quarry.log", "line1\nline2\nline3\n")
    result = _run_tail(logs, log_lines="2")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "line2" in result.stdout
    assert "line3" in result.stdout
    assert "line1" not in result.stdout


def test_logs_tail_missing_log_is_graceful(tmp_path: Path) -> None:
    """An absent diagnostic log is reported plainly, exit 0 (bug class 2)."""
    logs = tmp_path / "logs"
    logs.mkdir()
    result = _run_tail(logs)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "no diagnostic log at" in result.stdout


def test_logs_tail_unreadable_log_is_not_reported_absent(tmp_path: Path) -> None:
    """A present-but-unreadable diagnostic log must NOT be reported as absent.

    ``[ -r ]`` is false for BOTH an absent file and a present-but-unreadable one
    (chmod 000). The old single readability check collapsed the two, printing
    "no diagnostic log" for a log that is present — mis-reporting a read
    failure as absence (the misleading-fallback class already closed in
    logs-errors.sh, bug class 2). The three-state check must surface the
    unreadable log distinctly on stderr, never claim it is absent, never print
    its (unread) contents, and still exit 0 — a diagnostic never gates.
    """
    if os.geteuid() == 0:
        pytest.skip("root bypasses chmod 000, so the unreadable path can't be forced")
    logs = tmp_path / "logs"
    _write_log(logs, "quarry.log", "secret daemon stderr line\n")
    target = logs / "quarry.log"
    target.chmod(0o000)
    try:
        result = _run_tail(logs)
    finally:
        target.chmod(0o644)  # restore so tmp_path cleanup can remove it
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "no diagnostic log" not in result.stdout, "must not claim absent"
    assert "secret daemon stderr line" not in result.stdout, "unread log not printed"
    assert "present but unreadable" in result.stderr, "the unreadable log is surfaced"
    assert str(target) in result.stderr, "the path of the unreadable log is named"


def test_logs_tail_non_integer_log_lines_does_not_hide_present_log(
    tmp_path: Path,
) -> None:
    """A junk LOG_LINES falls back to 40 — a present log is never mis-reported.

    Without the guard, ``tail -n junk`` fails and the old ``|| echo`` fallback
    lied "no diagnostic log" even though the log existed. The guard must warn
    and still tail the present log.
    """
    logs = tmp_path / "logs"
    _write_log(logs, "quarry.log", "hello world\n")
    result = _run_tail(logs, log_lines="junk")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "is not a non-negative integer" in result.stderr
    assert "hello world" in result.stdout
    assert "no diagnostic log" not in result.stdout
